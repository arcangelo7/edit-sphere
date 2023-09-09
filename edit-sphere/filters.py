from __future__ import annotations

from typing import Tuple


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
    
    def split_ns(self, ns: str) -> Tuple[str, str]:
        last_part = ns.split('/')[-1].split('#')[-1]
        first_part = ns.replace(last_part, '')
        return first_part, last_part