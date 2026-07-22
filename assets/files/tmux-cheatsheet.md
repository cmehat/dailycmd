# tmux cheatsheet

`Ctrl-b` is the **prefix**. Notation `C-b d` = press `Ctrl-b`, release, then press `d`.

## Survive a dropped SSH connection (the main use case)
```bash
tmux new -s work          # start a named session
# ... launch your long-running command inside ...
# C-b d                   # detach — command keeps running on the server
# (connection can now die safely)

tmux attach -t work       # reattach later
tmux attach -d -t work    # force-attach, detaching any stale client
```

## Sessions
| Action | Command / keys |
|--------|----------------|
| New named session | `tmux new -s NAME` |
| List sessions | `tmux ls` |
| Attach to one | `tmux attach -t NAME` |
| Force attach (kick others) | `tmux attach -d -t NAME` |
| Detach (from inside) | `C-b d` |
| Rename session | `C-b $` |
| Kill a session | `tmux kill-session -t NAME` |
| Kill the server (all) | `tmux kill-server` |
| Switch session (inside) | `C-b s` then arrows |

## Windows (like tabs)
| Action | Keys |
|--------|------|
| New window | `C-b c` |
| Next / previous | `C-b n` / `C-b p` |
| Go to window N | `C-b 0`…`9` |
| Rename window | `C-b ,` |
| Close window | `C-b &` (or just `exit`) |
| List / pick window | `C-b w` |

## Panes (splits)
| Action | Keys |
|--------|------|
| Split vertical (left/right) | `C-b %` |
| Split horizontal (top/bottom) | `C-b "` |
| Move between panes | `C-b` + arrow keys |
| Cycle panes | `C-b o` |
| Toggle zoom (fullscreen pane) | `C-b z` |
| Close pane | `C-b x` (or `exit`) |
| Convert pane to window | `C-b !` |

## Scrolling & copy mode
| Action | Keys |
|--------|------|
| Enter scroll/copy mode | `C-b [` |
| Scroll | arrows / PgUp / PgDn |
| Search up / down | `?` / `/` (in copy mode) |
| Quit copy mode | `q` |

## Handy
| Action | Keys |
|--------|------|
| Command prompt | `C-b :` |
| List all key bindings | `C-b ?` |
| Reload config | `C-b :` then `source-file ~/.tmux.conf` |

## Watch a long job after reattaching
```bash
tail -f my-long-job-*.log           # Ctrl-c to stop tailing (job keeps running)
```

## Minimal `~/.tmux.conf` niceties (optional)
```tmux
set -g history-limit 100000      # bigger scrollback
set -g mouse on                  # mouse scroll/select panes
setw -g mode-keys vi             # vi keys in copy mode
```
