from handy_worker import disfluency


def test_counts_crisper_style_fillers() -> None:
    s = disfluency.summary("[UM] so I- I think that that we we should [UH] ship it")
    assert s == {"words": 11, "fillers": 2, "false_starts": 1, "repetitions": 1}


def test_discourse_fillers_need_a_comma() -> None:
    assert disfluency.summary("I like it, you know him")["fillers"] == 0
    assert disfluency.summary("it was, like, fine, you know, really")["fillers"] == 2


def test_highlight_without_colour_marks_each_kind() -> None:
    out = disfluency.highlight("um we we sh- should go", color=False)
    assert out == "[um] we {we} <sh-> should go"


def test_highlight_with_colour_uses_ansi() -> None:
    assert "\033[" in disfluency.highlight("uh hi", color=True)


def test_lone_dash_marks_previous_word_cut_off() -> None:
    assert disfluency.summary("we — we went")["false_starts"] == 1
