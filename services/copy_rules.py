"""One copy standard for every article and story generation path.

Field-specific prompts still define the output shape. Keep the common rules
here so a model or prompt change cannot silently omit them from one stage.
"""

COPY_RULES_VERSION = "2026-10-25.3"

COPY_RULES = """Sift copy rules:
- State supported facts directly. One useful sentence is enough. Never fill a
  sentence quota with an observation about importance, broader tensions,
  implications, challenges, or what a development highlights or underscores.
- Do not narrate the text with "the article explores/discusses/examines".
  If a source is only a viewing guide, prediction teaser, or promotion with no
  substantive supplied details, return an empty summary. Do not invent its advice.
  Background cannot add article-specific policy conditions, enforcement steps,
  venue restrictions, or guarantees absent from the supplied text.
  Preserve specific details, figures, uncertainty, and named attribution.
  Keep opinion attributed as opinion. A proposal is not an adopted policy;
  an event preceding a death does not establish the cause of death.
  Keep ongoing events ongoing: a halftime lead is not a completed win.
- Preserve proper-name spelling and actual relationships. A participant in an
  undercard is not necessarily fighting the headliner. Do not replace a named
  person with another person from memory or silently expand an unknown name.
  Do not recycle vague older source language: report the actual update, poll
  finding or performance fact, or omit it. "Reveals insights", "disappointing
  update" and "significant shift" without the finding are not useful facts.
  Puzzle-hint/answer teasers with no reported event also require an empty summary.
- Do not invent history, motives, causal links, predictions, or missing facts.
  Every figure and calendar date must appear in the supplied text. Do not
  infer a date from an event name such as "midterm elections".
  An excerpt's silence cannot establish what the full article omitted. Even
  an upstream generated summary's omission claim is not proof. Never write
  "not mentioned in the other input" as a coverage difference.
- Optional significance notes must add a concrete supported fact beyond the
  summary, not repeat it in different words or repeat its arithmetic.
  When nothing additional is supported, return an empty string.
- Background must explain a specific mechanism, procedure, institution, or
  difficult term beyond the summary. Do not repeat the news or a definition
  already in the terms list. Omit general topic overviews and obvious terms.
- Story notes must copy a distinct, contiguous passage from that source's
  supplied headline or summary, in at most 25 words. Do not add an introduction
  such as "emphasizes" or "explicitly states". Do not describe an unspecified
  location or other missing information as an outlet's angle.
  Do not assign an angle to every outlet. Blank notes are correct when the
  supplied inputs offer no difference. Keep every real source, once, even
  when its note is empty; never invent a source. Related reports need not
  share an event. Keep headline uncertainty once, without duplicate hedging.
"""
