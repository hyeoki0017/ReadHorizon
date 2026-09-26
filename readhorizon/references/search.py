"""한국어 친화 BM25 검색.

형태소 분석기 없이도 동작하도록 한글은 글자 2-gram, 영문·숫자는 단어 단위로 자른다.
옛한글 자모(ㆍ 등)도 한글 범위에 포함한다.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

# 한글 음절, 자모, 호환 자모, 옛한글 확장 자모 A·B
_HANGUL_RANGES = ((0xAC00, 0xD7A3), (0x1100, 0x11FF), (0x3130, 0x318F), (0xA960, 0xA97F), (0xD7B0, 0xD7FF))
_HANGUL = "".join(f"{chr(start)}-{chr(end)}" for start, end in _HANGUL_RANGES)
TOKEN_RE = re.compile(rf"[0-9a-z]+|[{_HANGUL}]+")
LATIN_RE = re.compile(r"[0-9a-z]")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in TOKEN_RE.findall(unicodedata.normalize("NFC", text).lower()):
        if LATIN_RE.match(word) or len(word) == 1:
            tokens.append(word)
        else:
            tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
    return tokens


class BM25:
    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.doc_tokens = [Counter(tokenize(d)) for d in documents]
        self.doc_len = [sum(c.values()) for c in self.doc_tokens]
        self.avg_len = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        df: Counter[str] = Counter()
        for counts in self.doc_tokens:
            df.update(counts.keys())
        n = len(documents)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = tokenize(query)
        results: list[float] = []
        for counts, length in zip(self.doc_tokens, self.doc_len, strict=True):
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self.avg_len or 1))
                score += self.idf.get(term, 0.0) * tf * (self.k1 + 1) / denom
            results.append(score)
        return results

    def top(self, query: str, k: int) -> list[tuple[int, float]]:
        ranked = sorted(enumerate(self.scores(query)), key=lambda item: item[1], reverse=True)
        return [(i, s) for i, s in ranked[:k] if s > 0]
