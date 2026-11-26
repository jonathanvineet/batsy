"""The 3D printing company, run by Batsy.

Everything about the business lives here, apart from Batsy's own printer/slicer code (which it only uses):

  db.py        business.db: customers, orders, parts, the print queue, every print run, spools, expenses, staff
  costing.py   the price of a part, and what a print really cost (or lost, when it was stopped)
  gcode.py     how much filament a G-code has used by line N (for the loss of a stopped print)
  quote.py     the virtual slicer: a file in -> instant estimate -> real OrcaSlicer slice -> price
  jobs.py      the queue: confirm -> queued, your order, slicing with the changes asked for, printing, and a
               watcher that records how every print ended
  brain.py     the business brain: the local model answers what it can, Claude Code (on this workstation) the rest
  api.py       /api/shop/... on Batsy's server (port 8765): PrintDesk Pro, the phone app and the cockpit use it
  cli.py       `batsy shop ...`

Who's who: you (the laptop's Tailscale login, or the paired phone) are the admin: you print, reorder, set prices.
Staff are tailnet logins you add; they make quotes and orders and see the queue. PrintDesk Pro runs on this
workstation and passes each visitor's tailnet IP through, so Batsy knows who they are.
"""
