from __future__ import annotations

from typing import Tuple
from urllib.parse import urlparse

import dateutil
import validators
from flask_babel import format_datetime, lazy_gettext


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
    
    def human_readable_primary_source(self, primary_source: str|None) -> str:
        if primary_source is None:
            return lazy_gettext('Unknown')
        if '/prov/se' in primary_source:
            version_url = f"/entity-version/{primary_source.replace('/prov/se', '')}"
            return f"<a href='{version_url}' alt='{lazy_gettext('Link to the primary source description')}'>" + lazy_gettext('Version') + ' ' + primary_source.split('/prov/se/')[-1] + '</a>'
        else:
            if validators.url(primary_source):
                return f"<a href='{primary_source}' alt='{lazy_gettext('Link to the primary source description')} target='_blank'>{primary_source}</a>"
            else:
                return primary_source