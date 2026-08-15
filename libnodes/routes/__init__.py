"""HTTP routes, one module per view.

Full-page handlers return the shell; everything else returns a bare fragment that
renders standalone with no parent context. That is the whole HTMX contract and the
design depends on it — a fragment that needs `base.html` cannot be swapped in.
"""
