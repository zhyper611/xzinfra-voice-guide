SENTENCE_ENDINGS = frozenset("。！？!?")


def limit_spoken_answer(answer: str, *, max_chars: int) -> str:
    normalized = answer.strip()
    if len(normalized) <= max_chars:
        return normalized

    endings = [
        index + 1
        for index, character in enumerate(normalized)
        if character in SENTENCE_ENDINGS
    ]
    within_limit = [position for position in endings if position <= max_chars]
    if within_limit:
        return normalized[: within_limit[-1]].strip()
    if endings:
        return normalized[: endings[0]].strip()
    return normalized
