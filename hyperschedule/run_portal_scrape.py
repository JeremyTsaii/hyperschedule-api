#!/usr/bin/env python3

import argparse
import json
import os
import sys

import hyperschedule.libportal as libportal
import hyperschedule.util as util

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    util.add_boolean_arg(parser, "headless")
    util.add_boolean_arg(parser, "kill-chrome")
    args = parser.parse_args()
    json.dump(libportal.get_latest_course_list({
        "headless": args.headless,
        "kill_chrome": args.kill_chrome,
        "lingk_key": os.environ.get("HYPERSCHEDULE_LINGK_KEY"),
        "lingk_secret": os.environ.get("HYPERSCHEDULE_LINGK_SECRET"),
    }), sys.stdout)