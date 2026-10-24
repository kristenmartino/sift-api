"""One copy standard for every article and story generation path.

Field-specific prompts still define the output shape. Keep the common rules
here so a model or prompt change cannot silently omit them from one stage.
"""

COPY_RULES_VERSION = "2026-10-24.1"

COPY_RULES = """Sift copy rules:
- State supported facts directly. One useful sentence is enough. Never fill a
  sentence quota with an observation about importance, broader tensions,
  implications, challenges, or what a development highlights or underscores.
- Do not narrate the text with "the article explores/discusses/examines".
  Preserve specific details, figures, uncertainty, and named attribution.
  Keep opinion attributed as opinion. A proposal is not an adopted policy;
  an event preceding a death does not establish the cause of death.
- Do not invent history, motives, causal links, predictions, or missing facts.
  An excerpt's silence cannot establish what the full article omitted. Even
  an upstream generated summary's omission claim is not proof. Never write
  "not mentioned in the other input" as a coverage difference.
- Optional significance notes must add a concrete supported fact beyond the
  summary, not repeat it in different words or repeat its arithmetic.
  When nothing additional is supported, return an empty string.
- Background must explain a specific mechanism, procedure, institution, or
  difficult term beyond the summary. Do not repeat the news or a definition
  already in the terms list. Omit general topic overviews and obvious terms.
- Story notes must name a distinct reported detail in at most 25 words.
  Do not assign an angle to every outlet. Blank notes are correct when the
  supplied inputs offer no difference. Keep every real source, once, even
  when its note is empty; never invent a source. Related reports need not
  share an event. Keep headline uncertainty once, without duplicate hedging.
"""
