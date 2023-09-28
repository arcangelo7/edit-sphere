from __future__ import annotations

from typing import Tuple
from urllib.parse import urlparse

import dateutil
from flask_babel import format_datetime


class Filter:
    def __init__(self, context):
        self.context = context

    def human_readable_predicate(self, url):
        first_part, last_part = self.split_ns(url)
        if first_part in self.context:
            if last_part.islower():
                return last_part
            else:
                words = []
                word = ""
                for char in last_part:
                    if char.isupper() and word:
                        words.append(word)
                        word = char
                    else:
                        word += char
                words.append(word)
                return " ".join(words).lower()
        else:
            return url
    
    def human_readable_datetime(self, dt_str):
        dt = dateutil.parser.parse(dt_str)
        return format_datetime(dt, format='long')

    def split_ns(self, ns: str) -> Tuple[str, str]:
        parsed = urlparse(ns)
        if parsed.fragment:
            first_part = parsed.scheme + '://' + parsed.netloc + parsed.path + '#'
            last_part = parsed.fragment
        else:
            first_part = parsed.scheme + '://' + parsed.netloc + '/'.join(parsed.path.split('/')[:-1]) + '/'
            last_part = parsed.path.split('/')[-1]
        return first_part, last_part