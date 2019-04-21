#!/usr/bin/env python3

import argparse
import collections
import copy
import datetime
import http
import http.server
import itertools
import json
import json.decoder
import os
import pathlib
import re
import string
import subprocess
import threading
import traceback

import requests

import hyperschedule.libcourse as libcourse
import hyperschedule.util as util

from hyperschedule.util import ScrapeError, log, die

DIR = pathlib.Path(__file__).resolve().parent

## Thread-global variables

INITIAL_COURSE_DATA = {
    "current": None,
    "index": None,
    "initial_timestamp": None,
    "updates": [],
    "timestamp": None,
}

thread_lock = threading.Lock()
course_data = copy.deepcopy(INITIAL_COURSE_DATA)

## Course data retrieval

# This should probably only be using attributes in COURSE_INDEX_ATTRS,
# so that any two non-distinct courses (see the API documentation)
# have the same formatted course code.
def format_course_code(course):
    return "{} {:03d}{} {}-{:02d}".format(
        course["department"],
        course["courseNumber"],
        course["courseCodeSuffix"],
        course["school"],
        course["section"])

def deduplicate_course_keys(courses):
    """
    Given a list of parsed course objects, deduplicate keys. This
    means that course objects are modified in-place so that no two
    course objects will have equal keys (see `format_course_code`).
    The modification is to assign each "duplicate" course with a
    suffix letter A, B, C, etc. If any of the problematic courses
    already have suffixes, then abort with ScrapeError.

    This function is unfortunately necessary because Portal sometimes
    (as of March 2019) includes multiple courses with identical course
    codes (I'm looking at you, PSYC131 JT-01).
    """
    course_index = collections.defaultdict(list)
    for course in courses:
        course_index[libcourse.course_to_index_key(course)].append(course)
    for key, courses in course_index.items():
        if len(courses) > 1:
            for course in courses:
                if course["courseCodeSuffix"]:
                    raise ScrapeError(
                        "duplicate course with suffix: {}"
                        .format(repr(format_course_code(course))))
            for course, letter in zip(courses, string.ascii_uppercase):
                course["courseCodeSuffix"] = letter

def index_courses(courses):
    deduplicate_course_keys(courses)
    course_index = {}
    for course in courses:
        key = libcourse.course_to_index_key(course)
        if key in course_index:
            raise ScrapeError("more than one course matching {}"
                              .format(repr(format_course_code(course))))
        course_index[key] = course
    return course_index

def compute_update(old_courses_index, new_courses_index):
    old_courses_keys = set(old_courses_index)
    new_courses_keys = set(new_courses_index)
    maybe_modified_keys = new_courses_keys & old_courses_keys
    modified_keys_and_attrs = {}
    for key in maybe_modified_keys:
        old_course = old_courses_index[key]
        new_course = new_courses_index[key]
        attrs = []
        for attr in libcourse.COURSE_ATTRS:
            if old_course[attr] != new_course[attr]:
                attrs.append(attr)
        if attrs:
            modified_keys_and_attrs[key] = attrs
    return {
        "added": list(new_courses_keys - old_courses_keys),
        "removed": list(old_courses_keys - new_courses_keys),
        "modified": modified_keys_and_attrs,
    }

def compute_diff(since):
    added = set()
    removed = set()
    modified = {}
    for timestamp, update in course_data["updates"]:
        if timestamp > since:
            for key, attrs in update["modified"].items():
                assert key not in removed
                if key not in modified:
                    modified[key] = set()
                modified[key] |= set(attrs)
            for key in update["added"]:
                assert key not in modified and key not in added
                if key in removed:
                    removed.remove(key)
                    modified[key] = set(libcourse.COURSE_ATTRS)
                else:
                    added.add(key)
            for key in update["removed"]:
                assert key not in removed
                if key in modified:
                    del modified[key]
                if key in added:
                    added.remove(key)
                else:
                    removed.add(key)
    index = course_data["index"]
    added_courses = []
    for key in added:
        added_courses.append(index[key])
    removed_courses = []
    for key in removed:
        removed_courses.append(libcourse.course_from_index_key(key))
    modified_courses = []
    for key in modified:
        current_course = index[key]
        course = {}
        attrs = modified[key]
        for attr in itertools.chain(libcourse.COURSE_INDEX_ATTRS, attrs):
            course[attr] = current_course[attr]
        modified_courses.append(course)
    return {
        "added": added_courses,
        "removed": removed_courses,
        "modified": modified_courses,
    }

MAX_UPDATES_SAVED = 100

def update_course_data(timestamp, courses, index, malformed_courses):
    global course_data
    with thread_lock:
        last_index = course_data["index"]
        if last_index is not None:
            updates = course_data["updates"]
            # Use a list so we can serialize to JSON.
            updates.append([timestamp, compute_update(last_index, index)])
            if len(updates) > MAX_UPDATES_SAVED:
                updates[:] = updates[len(updates) - MAX_UPDATES_SAVED:]
        else:
            course_data["initial_timestamp"] = timestamp
        course_data["current"] = courses
        course_data["index"] = index
        course_data["timestamp"] = timestamp
        course_data["malformed"] = malformed_courses

def fetch_and_update_course_data(config):
    timestamp = int(datetime.datetime.now().timestamp())
    args = []
    args.append("--headless" if config["headless"] else "--no-headless")
    args.append(
        "--kill-chrome" if config["kill_chrome"] else "--no-kill-chrome")
    process = subprocess.Popen(
        ["python", "-m", "hyperschedule.run_portal_scrape", *args],
        stdout=subprocess.PIPE)
    try:
        output, _ = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        output = process.communicate()
        print(output[0].decode(errors="backslashreplace"), end="")
        raise ScrapeError("timed out")
    if process.returncode != 0:
        raise ScrapeError("error in portal scraper")
    courses, malformed_courses = json.loads(output)
    update_course_data(
        timestamp, courses, index_courses(courses), malformed_courses)

def write_course_data_to_cache_file():
    log("Writing course data to cache on disk...")
    with open(COURSE_DATA_CACHE_FILE, "w") as f:
        json.dump(course_data, f)
    with open(COURSE_DATA_PRETTY_CACHE_FILE, "w") as f:
        json.dump(course_data, f, indent=2)
    log("Finished writing course data to disk.")

COURSE_DATA_CACHE_FILE = os.path.join(
    os.path.dirname(__file__), "out/course-data.json")

COURSE_DATA_PRETTY_CACHE_FILE = os.path.join(
    os.path.dirname(__file__), "out/course-data-pretty.json")

last_dms_update = None

def run_single_fetch_task(config):
    global last_dms_update
    try:
        log("Starting course data update...")
        fetch_and_update_course_data(config)
        if config["use_cache"]:
            write_course_data_to_cache_file()
    except Exception:
        log("Failed to update course data:\n"
            + traceback.format_exc().rstrip())
        return False
    else:
        log("Finished course data update.")
        if last_dms_update:
            now = datetime.datetime.now()
            delta = now - last_dms_update
            long_enough = delta > datetime.timedelta(minutes=5)
        else:
            long_enough = True
        if config["use_snitch"] and long_enough:
            log("Updating Dead Man's Snitch...")
            # Let the NSA know we finished updating the course data.
            # Radon will get an email if this code doesn't get run for
            # more than an hour.
            resp = requests.get("https://nosnch.in/f08b6b7be5")
            last_dms_update = datetime.datetime.now()
            log("Finished updating Dead Man's Snitch {}".format(resp))
        return True

def run_fetch_task(config):
    delay = config["delay"]
    if run_single_fetch_task(config):
        log("Updating again after {:.0f} seconds.".format(delay))
    else:
        log("Trying again after {:.0f} seconds.".format(delay))
    thread = threading.Timer(delay, lambda: run_fetch_task(config))
    thread.start()

## Server

ERROR_MESSAGE_FORMAT = """\
<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8">
  </head>
  <body>
    <h1>%(code)d %(message)s</h1>
    <p>%(explain)s.</p>
  </body>
</html>
"""

class HTTPServer(http.server.ThreadingHTTPServer):

    def __init__(self, attrs, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key, value in attrs.items():
            setattr(self, key, value)

class HTTPHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/":
            with open("index.html", "rb") as f:
                html = f.read()
                self.send_response(http.HTTPStatus.OK)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html)
            return
        match = re.match(r"/api/v2/all-courses/?", self.path)
        if match:
            with thread_lock:
                if course_data["current"]:
                    courses = course_data["current"]
                    timestamp = course_data["timestamp"]
                    response = {
                        "courses": courses,
                        "timestamp": timestamp,
                        "malformedCourseCount":
                        len(course_data["malformed"]),
                    }
                    response_body = json.dumps(response).encode()
                    self.send_response(http.HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(response_body)
                else:
                    self.send_error(http.HTTPStatus.SERVICE_UNAVAILABLE,
                                    explain=("The course data is not yet "
                                             "available. Please wait"))
            return
        match = re.match(r"/api/v2/courses-since/([-0-9]+)/?", self.path)
        if match:
            with thread_lock:
                try:
                    since = int(match.group(1))
                except ValueError:
                    self.send_error(
                        http.HTTPStatus.BAD_REQUEST,
                        explain=("Malformed timestamp {}"
                                 .format(repr(match.group(1)))))
                    return
                timestamp = course_data["timestamp"]
                if ((course_data["current"]
                     and since >= course_data["initial_timestamp"])):
                    diff = compute_diff(since)
                    response = {
                        "incremental": True,
                        "diff": diff,
                        "timestamp": timestamp,
                        "malformedCourseCount":
                        len(course_data["malformed"]),
                    }
                elif course_data["current"]:
                    response = {
                        "incremental": False,
                        "courses": course_data["current"],
                        "timestamp": timestamp,
                        "malformedCourseCount":
                        len(course_data["malformed"]),
                    }
                else:
                    self.send_error(http.HTTPStatus.SERVICE_UNAVAILABLE,
                                    explain=("The course data is not yet "
                                             "available. Please wait"))
                    return
                response_body = json.dumps(response).encode()
            self.send_response(http.HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response_body)
            return
        match = re.match(r"/api/v2/malformed-courses/?", self.path)
        if match:
            with thread_lock:
                if course_data["current"]:
                    response_body = json.dumps(
                        course_data["malformed"]).encode()
                    self.send_response(http.HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(response_body)
                else:
                    self.send_error(http.HTTPStatus.SERVICE_UNAVAILABLE,
                                    explain=("The course data is not yet "
                                             "available. Please wait"))
            return
        match = re.match(r"/experimental/course-data/?", self.path)
        if match:
            with thread_lock:
                response_body = json.dumps(course_data).encode()
            self.send_response(http.HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response_body)
            return
        self.send_error(http.HTTPStatus.NOT_FOUND)
        return

    def do_PUT(self):
        global course_data
        if not self.server.debug:
            self.send_error(http.HTTPStatus.NOT_IMPLEMENTED,
                            message="Unsupported method ('PUT')")
            return
        match = re.match(r"/debug/set-courses(?:/([-0-9]+))?/?", self.path)
        if match:
            timestamp = int(match.group(1)
                            or datetime.datetime.now().timestamp())
            content_length = int(
                self.headers.get("Content-Length", 0))
            courses = json.loads(self.rfile.read(content_length).decode())
            index = index_courses(courses)
            update_course_data(timestamp, courses, index)
            if self.server.use_cache:
                write_course_data_to_cache_file()
            self.send_response(http.HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if self.path.rstrip("/") == "/debug/scrape":
            t = threading.Thread(
                target=lambda: run_single_fetch_task(
                    self.server.headless,
                    self.server.use_cache))
            t.start()
            self.send_response(http.HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if self.path.rstrip("/") == "/debug/reset":
            with thread_lock:
                course_data = INITIAL_COURSE_DATA
                if self.server.use_cache:
                    write_course_data_to_cache_file()
            self.send_response(http.HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_error(http.HTTPStatus.NOT_FOUND)
        return

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        http.server.BaseHTTPRequestHandler.end_headers(self)

    def log_message(self, format, *args):
        log(format % args)

    def log_request(self, code="-", size="-"):
        self.log_message("{} : {} {}".format(code, self.command, self.path))

    error_message_format = ERROR_MESSAGE_FORMAT

def main():
    global course_data
    port = os.environ.get("PORT", "3000")
    try:
        port = int(port)
    except ValueError:
        die("malformed PORT: {}".format(repr(port)))
    parser = argparse.ArgumentParser()
    util.add_boolean_arg(
        parser, "production",
        yes_args=["--prod", "--production"],
        no_args=["--dev", "--develop", "--development"])
    util.add_boolean_arg(parser, "headless", default=True)
    util.add_boolean_arg(parser, "cache", default=None)
    util.add_boolean_arg(parser, "scrape", default=True)
    util.add_boolean_arg(parser, "snitch", default=False)
    util.add_boolean_arg(parser, "kill-chrome", default=False)
    args = parser.parse_args()
    if args.cache is None:
        args.cache = not args.production
    if args.cache:
        try:
            log("Loading cached course data from disk...")
            with open(COURSE_DATA_CACHE_FILE) as f:
                course_data = json.load(f)
            log("Finished loading cached course data.")
        except FileNotFoundError:
            pass
        except json.decoder.JSONDecodeError:
            log("Failed to load cached course data due to JSON parse error.")
    if args.scrape:
        thread = threading.Thread(
            target=lambda: run_fetch_task({
                "delay": 5,
                "use_cache": args.cache,
                "use_snitch": args.snitch,
                "kill_chrome": args.kill_chrome,
                "headless": args.headless,
            }), daemon=True)
        thread.start()
    httpd = HTTPServer({
        "debug": not args.production,
        "headless": args.headless,
        "use_cache": args.cache,
    }, ("", port), HTTPHandler)
    log("Starting server on port {}...".format(port))
    httpd.serve_forever()

if __name__ == "__main__":
    main()

# Local Variables:
# outline-regexp: "^##+"
# End: