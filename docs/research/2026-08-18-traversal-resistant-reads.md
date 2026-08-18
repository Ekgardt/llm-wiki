# Traversal-resistant reads: what current implementations actually forbid

Date: 2026-08-18. Question: `read_stable_bytes` refuses any symbolic link among
a path's ancestors. On macOS `/var` is a symlink to `/private/var`, so every
temporary file sits behind one and the reader is unusable there. Is blanket
rejection the current best practice, or is there a stronger contract?

## What the current primitives do

**Linux `openat2(2)`** takes an `open_how` whose `resolve` flags are applied
atomically during lookup. `RESOLVE_NO_SYMLINKS` blocks symlinks in *every*
component — the strictest option — while `RESOLVE_BENEATH` blocks only
resolutions that escape the starting directory, so a symlink pointing inside
the tree is followed normally. The manual page notes that applications using
`RESOLVE_NO_SYMLINKS` "are encouraged to make its use configurable, unless
[it is] employed for a specific security purpose", because symbolic links are
widely used by end users.

**FreeBSD** replaced the flawed `O_BENEATH` with `O_RESOLVE_BENEATH` in 13.0,
whose semantics were aligned with Linux's `RESOLVE_BENEATH`.

**macOS 11+** offers `O_NOFOLLOW_ANY`, which fails the open when any component
is a symlink. It is the exact analogue of the rule this reader implements by
hand, and it exists precisely because the per-component `O_NOFOLLOW` only
covers the final component.

**Go 1.24 `os.Root`** is the highest-level example. It permits relative
components and symlinks that do not escape the root, and states its threat
model explicitly: it defends against filesystem constructs an ordinary user can
create — symlinks above all — and does not defend against ones that require
root, such as bind mounts.

## What that means here

The industry moved from "no symlinks anywhere" to "no escape from a trusted
root". The strict flag survives for targeted uses, not as the default posture,
and the reason is exactly the situation this reader hit: legitimate system
layouts contain symlinks, and refusing them refuses the platform.

The distinction that carries the security weight is *who could have created the
link*. A symlink an unprivileged user can replace is the attack. A symlink the
operating system's installer placed, in a directory only root can write, is the
platform. Go names that boundary directly; `openat2` implies it by making the
strict flag opt-in.

## Applied decision

`read_stable_bytes` accepts an ancestor symlink only when the link belongs to
root and the directory containing it belongs to root and is not group- or
other-writable. Everything else is rejected as before, the final component must
still be a regular non-symlink file, and the open still uses `O_NOFOLLOW` with
full identity verification before and after the read. Symlink mode bits are
meaningless on Linux, so the containing directory carries the decision.

Recorded in `knowledge/notes/system-symlink-ancestor-decision.md`.

## Sources

- `openat2(2)`, Linux manual page — resolve flags, `RESOLVE_NO_SYMLINKS`
  guidance: https://man7.org/linux/man-pages/man2/openat2.2.html
- "Traversal-resistant file APIs", The Go Blog — `os.Root` semantics and threat
  model: https://go.dev/blog/osroot
- golang/go#73079 — `O_NOFOLLOW_ANY` availability on macOS 11+:
  https://github.com/golang/go/issues/73079
- "Restricting path name lookup with openat2()", LWN:
  https://lwn.net/Articles/796868/
