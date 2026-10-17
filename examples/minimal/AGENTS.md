# Working rules

- Export a stable `LOCKSTEP_AGENT=<client>:<session id>` in every shell; claims, jobs and log entries are recorded under it.
- Project state comes from `lockstep status`; pick work with `lockstep ready`.
- Run `lockstep claim <node> --why "<plan>"` before starting, and `lockstep release <node> --outcome ... --note ...` when you stop.
- New statements go in only through `lockstep node add`; never edit a statement by hand.
- Commands that run longer than a minute go through `lockstep job run --why "<purpose>" --out runs/<dir> -- <command>`.
