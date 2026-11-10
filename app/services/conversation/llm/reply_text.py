"""Keep spoken chat output within the child's sentence/word budget."""

import re


def short_reply(text: str) -> str:
    # Remove presentation-only list prefixes, never cut a word mid-sentence.
    text = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", "", text).strip()
    text = text.replace("**", "").replace("`", "")
    sentences = re.findall(r".+?(?:[.!?…]+[\"»”']*(?=\s|[^\W\d_]|$)|$)", text, re.DOTALL)
    kept: list[str] = []
    words = questions = 0
    for sentence in sentences:
        sentence = " ".join(sentence.split())
        count = len(sentence.split())
        if not sentence:
            continue
        if len(kept) == 3 or words + count > 35 or len(sentence) > 500 or questions + sentence.count("?") > 1:
            break
        kept.append(sentence)
        words += count
        questions += sentence.count("?")
    if not kept:
        raise ValueError("No complete short response within speech budget")
    return " ".join(kept)
