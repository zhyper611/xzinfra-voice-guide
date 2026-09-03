from showroom_guide.answer_text import limit_spoken_answer


def test_short_answer_is_preserved():
    assert limit_spoken_answer("这是简短回答。", max_chars=20) == "这是简短回答。"


def test_long_answer_stops_at_last_complete_sentence_within_limit():
    answer = "第一句介绍核心能力。第二句说明工作过程。第三句补充更多细节。"

    limited = limit_spoken_answer(answer, max_chars=20)

    assert limited == "第一句介绍核心能力。第二句说明工作过程。"


def test_first_complete_sentence_may_exceed_limit_instead_of_being_cut():
    first_sentence = "这是一句完整但长度超过安全上限的讲解内容。"
    answer = first_sentence + "后续内容不再播报。"

    assert limit_spoken_answer(answer, max_chars=10) == first_sentence


def test_unpunctuated_answer_is_preserved_instead_of_creating_broken_sentence():
    answer = "没有任何句末标点的连续文本"

    assert limit_spoken_answer(answer, max_chars=5) == answer
