# TypeScript navigation fixture

This is a **fixture**, not a real repository. There is no TypeScript project on
this machine (`find / -name tsconfig.json -not -path '*/node_modules/*'`
returned nothing on 2026-08-28), so the TypeScript navigation evidence in
`tests/test_typescript_navigation.py` is produced against these three files.

It is deliberately the smallest project that can tell a loaded project from an
unloaded one: `main.ts` uses `renewLease` twice and imports it from `lease.ts`,
so before the project graph exists the server answers go-to-definition with the
*import binding* in `main.ts` and finds 3 references, and afterwards it answers
with the declaration in `lease.ts` and finds 4. See
`docs/research/2026-08-28-precise-navigation-beyond-python.md`, Finding 4.

`module` is `ESNext` on purpose. With `ES2022` against `moduleResolution:
Bundler` the cross-file definition never resolves at all, which would make the
fixture pass for the wrong reason.
