# A headless launcher for the EMOTIV EPOC+

Connects the headset and keeps it connected, in the background, without the
EMOTIV Launcher application, without logging in, and without waiting on
Emotiv's cloud.

## Why

The EMOTIV Launcher is a graphical front end to a local background service,
`CortexService`. The service is what actually owns the headset. The Launcher is
needed to log in and to display things; it is not needed to use the hardware.

On this machine the Launcher fails in two ways, neither of which is a fault of
the headset:

1. **Account synchronisation.** The service log repeatedly records
   `[det_sync] SyncMetadata is not available for user <id> 500` — an HTTP 500
   returned by Emotiv's own servers for this account. Launcher login waits on
   that synchronisation, so it sits at *Connecting to server …*. This is the
   failure with direct evidence in the log, and nothing local can fix a fault
   on their side.

2. **A stale connection after the service restarts.** `CortexService` is a
   server and serves several clients at once, so two Launcher windows do not
   block each other. But when the service restarts, a window whose socket
   dropped does not always re-establish it; it then shows the same
   *Connecting to server …* message with a dead connection, while another
   window that did reconnect works normally. That is why the failure looks
   random — it depends on which window you are looking at.

This launcher sidesteps both. It speaks the service's local JSON-RPC interface
directly over its WebSocket port, never starts the graphical application, and
never waits on the account synchronisation.

## Use

Double-click **`Emotiv launcher.command`** for a menu, or from a terminal:

    ./launcherd.py status      what the service and headset are doing
    ./launcherd.py connect     connect the headset; show battery and contact quality
    ./launcherd.py start       run in the background, self-healing
    ./launcherd.py stop        stop the background instance
    ./launcherd.py restart-service   when the service itself is stuck

To have it running at every login:

    ./install-at-login.sh

## What it reports

    EMOTIV headset status (no Launcher involved)
    ----------------------------------------------------
      service                running, pid [12345]
      service answering      yes
      cortex version         4.8.8.5
      account                <your emotiv account>
      headset                EPOCPLUS-XXXXXXXX  connected  via dongle  fw 0x0633
      battery                28%
      contact quality        AF3:0  F7:0  F3:0  ...  AF4:0  OVERALL:0

**Check contact quality before every recording.** It is the one measurement
that tells you whether the electrodes are on the scalp properly, and it is not
recoverable from the raw packet stream — only Cortex reports it. All-zero means
the electrodes are not making contact and the recording will be noise, however
convincing the traces look on screen.

Running state lives in `~/.emotiv-launcher/`:

| file | contents |
|---|---|
| `status.json` | the latest reading, rewritten each poll |
| `launcherd.log` | what the daemon has done |
| `launcherd.pid` | the background instance, so only one ever runs |

## How it works

`cortex_ws.py` is a small WebSocket client written against the Python standard
library — the standard library has no WebSocket client, and this keeps the tool
dependency-free. It implements only what is needed: the opening handshake,
masked text frames out, frames in.

`launcherd.py` uses it to call the service directly. Every call is local. The
only file read from outside this folder is the client credentials, which are
needed for battery and contact quality; everything else works without them.

If the service stops answering on two consecutive checks, the daemon restarts
it by ending the process and letting launchd return it — the
`com.emotiv.cortex` daemon is configured `KeepAlive=1`, so this needs no
privileges. Restarts are rate-limited to one every two minutes.

## Requirements

macOS with the EMOTIV software installed (for `CortexService` itself) and
Python 3. No packages to install. Battery and contact quality additionally need
Cortex client credentials and a licence; without them the launcher still
connects the headset and reports everything else.
