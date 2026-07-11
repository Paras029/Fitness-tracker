"""ON-DEMAND only, like rating.py's answer_question() for nutrition --
answers a free-text question about the user's lab results, grounded in
the actual stored data (including any description/how_to_read text
captured from their reports, which takes priority over general
knowledge when present). Never diagnoses; punts to "ask a doctor" for
anything requiring real medical judgment.
"""

import json

from core.gemini.client import call


def answer_lab_question(question, results):
    """results: list of lab_results rows (latest value per test), each
    optionally carrying description/how_to_read from the source report."""
    prompt = (
        f'A user of a health-tracking app asked about their lab results: "{question}"\n\n'
        "Here is their current lab data -- each result may include description/how_to_read "
        "text captured directly from their own report; if present, ground your answer in "
        "that over general knowledge, since it reflects what their specific lab actually "
        "printed (reference ranges can vary by lab):\n" + json.dumps(results) + "\n\n"
        "Answer in 2-4 sentences using only this data. This is NOT medical advice -- if the "
        "question needs real medical judgment (e.g. \"should I be worried\", \"what medication "
        "should I take\"), say that plainly and suggest discussing it with a doctor rather than "
        "guessing. If the data doesn't actually answer the question, say what's missing instead "
        "of guessing. Plain text only, no markdown."
    )
    text = call([{"text": prompt}], want_json=False)
    return text.strip() if text else None
