# Internationalization and review policy

**English** · [简体中文](i18n-policy.md)

The user's settings select the interface language. The Profile's `language` selects the language of project files; a terminal locale must not rewrite existing files. Existing files keep their headings, and parsers accept both Chinese and English. User content, credentials, rules and source quotations are not interface messages and must not be translated automatically.

## Paired documentation gate

`tests/i18n-policy.json` records Chinese/English documentation pairs and an explicit backlog of existing untranslated files. Both files in a pair must exist and link to each other through relative Markdown links. New root README/INSTALL files, plugin Skill bodies and reference documents cannot silently enter a wildcard exception: register a pair, or record a specific deferred path and its reason. Changes to this registry require review too.

The nudge and inbox guides and this policy must remain paired. Existing Chinese-only documentation is translated incrementally; `deferred` means unfinished, not translated or reviewed. When editing a deferred document, prefer adding its English counterpart and moving it into the pairs list. The gate verifies files and links; it cannot establish semantic equivalence.

## Terminology and messages

The glossary fixes the interface names `Find files`, `Progress` and `Project guide`. Tests check standalone labels across the selected catalogs and references to labels in Chinese quotation marks. Mechanical rules do not infer ordinary prose casing or context. New glossary terms need a source label, an English name and applicable catalogs.

Existing tests continue to check `_()`/`N_()` wrapping, placeholders, untranslated literals, host locale matrices and command discovery. Template language selection and bilingual parsing require separate behavior tests; an absence of Chinese characters alone does not prove correctness.

## Human review is still pending

There is no recorded native-speaker review, so the English must not be described as having passed one. Future reviewers should record each document or catalog, commit SHA, review date, terminology, ambiguities and corrections. Never record token values. Focus on migration, destructive actions, preview/write boundaries and recovery instructions. Automated checks and model review do not replace this human review record.
