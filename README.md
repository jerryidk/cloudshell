# cloudshell

An interactive shell for finding CloudLab machines that are free for a time
range and reserving them. Works on Linux and macOS.

```
cloudlab> find tomorrow 9am 8h -n 2
cloudlab> /c6620                # search the output
cloudlab> reserve 25            # checks with CloudLab, then asks to confirm
cloudlab> mine                  # your project's reservations
```

## Install

Linux or macOS. Needs Python 3.9 or newer; the setup script checks this.

```sh
./setup.sh          # finds Python 3.9+, creates .venv, installs everything
./cloudshell
```

If no suitable Python is found, `setup.sh` tells you how to install one. If
[uv](https://docs.astral.sh/uv/) is installed, it uses uv to fetch Python
instead.

## First run

1. The first command that needs CloudLab asks for your **CloudLab username and
   password** (the ones you use on cloudlab.us). If you sign in through GENI or
   another federated login, set a CloudLab password first (cloudlab.us → your
   name → Change Password).
2. The password and the login session are saved in your system keychain
   (macOS Keychain, or GNOME Keyring / KWallet on
   Linux). They are never written to a plain file. Later runs reuse the
   session and log in again by themselves when it expires.
3. It asks once for your **project** (as shown on your CloudLab dashboard).
   To use a project subgroup, write `project/group`.

`login` enters different credentials, `logout` forgets the session, and
`logout --forget` also removes the saved password.

A failed login is never retried automatically, because repeated failures can
get your account or IP temporarily frozen by CloudLab.

## Commands

| command | what it does |
|---|---|
| `find [START] [LENGTH] [-n N] [-f REGEX]` | Free machine types for LENGTH (e.g. `4h`, `2d`) from START (`tomorrow 9am`, `12/2 15:00`, `fri 10:00`). No START means as soon as possible. `-n` sets the node count, and `-f` filters on specs (`-f gpu`, `-f wisc`, `-f 100G`). |
| `hw [-f REGEX]` | Hardware catalog with free/total counts right now |
| `show <#>` | All specs of one row |
| `reserve <#> [-r REASON]` | Reserve that row's slot from the last `find`. It is validated first, and you see whether it will be auto-approved before confirming. |
| `mine` | Your project's reservations |
| `cancel <id>` | Delete a reservation (an id prefix is fine) |
| `/TEXT` | Search the last output; `/` on its own shows everything again |
| `project NAME`, `refresh`, `help`, `quit` | |

## Where the data comes from

- Specs: the public sheet behind cloudlab.us/portal-hardware.php
- Availability: the reservation forecast behind cloudlab.us/resinfo.php
- Reservations: the same requests the cloudlab.us reservation form and
  dashboard make

These are the website's own requests, not a documented API, so a CloudLab site
update could break them.

## Optional: Portal API fallback

Run `./setup.sh --api` and put a Portal API token (cloudlab.us → your name →
Portal API Token) in `cloudlab.jwt` next to the script. If the web forecast is
unavailable, `find` then falls back to the Portal API search. That search is
much slower and sometimes times out.

## Files

Non-secret state (remembered username and project, command history, cached
hardware sheet) lives in `~/.cache/cloudshell` (Linux) or
`~/Library/Caches/cloudshell` (macOS). Set `CLOUDSHELL_HOME` to change it.
