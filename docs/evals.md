# Evals

Part of the [README](../README.md)'s reference.

The demo calls, run as text through the turn loop a live call uses, in `evals/`. Each
scenario file in `evals/scenarios/` holds the call's clock (`now`), the caller's turns, the
model's side for scripted mode, and what must come of the call. The 13 scenarios and the
commands are in the README's "Testing and evals".

- Each run gets a database of its own on the server the `POSTGRES_*` environment variables
  name (as for the tests, not `.env`): copied from a template migrated once, with a runtime
  role of the evals' own (`sva_eval_<random>_app`, which must not exist yet and is dropped at
  the end), seeded for the scenario's day. The database is dropped after the run.
- A run is one call as the live pipeline makes it: a call id from the call registry,
  `start_call` on the application's tools (`call_started`), each caller turn through
  `CallAgent.handle` (the input guard, the gate, dispatch, the scripted lines), then
  `CallAgent.end` (`call_ended`): for the reason the call's last phase gives, or as a caller who
  hangs up after the last turn. The phase a scenario expects is that last phase. The call's
  clock is the scenario's `now`; the audit and claim keys are random for the run.
- Expectations, after a turn and at the end of the call: the scripted lines said, by id; the
  tools the model asked for, in order, with chosen arguments and chosen values of what each
  returned; phase and verified; the rows the call wrote (table, count, column values, compared
  with the seed); the call's audit actions in order; strings never said nor shown to the
  model (in any case, and as digits when written in digit words); the tools offered, or never
  offered; and for a turn, the input guard's flags, whether the model was shown it, and the
  output guard's rule if it withheld part of the reply. At the end of the call, `record`: the
  call's stored record exists, ends for the call's reason, holds the markers of its withheld
  turns in order (`swallowed`, `emergency`) and none of the strings it forbids (used in `s01`
  and `s06`). A turn's `expect_scripted` holds
  expectations only scripted mode checks: what comes of a reply only the scripted model gives,
  such as advice the output guard withholds. A file with an unknown key, a key given twice, or
  an empty expectation block is refused.
- Scripted mode (`tests/test_evals.py` runs every file in CI): each turn's model replies come
  from the file, and a turn must use exactly its replies. It proves the harness, the gate, the
  guards, the tools and the rows; it does not prove the model would choose those replies.
- Real mode: the model of the settings (`ANTHROPIC_API_KEY`, `LLM_MODEL`, from the
  environment or `.env`); the files' replies are ignored and the same expectations apply,
  but for each turn's `expect_scripted`. It
  refuses without a key, under pytest and in CI (`CI` set), and is never run there. It prints
  each scenario, the runs that passed and, for a failed run, the first unmet expectation by
  name, never anything said; it exits non-zero on any failure.
- Limits: the expectations are exact, so a real model that asks a tool once more, or takes
  another path to the same end, fails the scenario. Real mode has no judge of the model's
  wording beyond the strings a scenario forbids. `s09` forbids the phrasings of an approval,
  not the word: `refill_no_promise` itself says "a refill is approved" to deny it.
