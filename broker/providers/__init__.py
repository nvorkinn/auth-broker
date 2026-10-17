"""Long-running processes that fetch what the screens want and write it to the shared Redis store.

Each runs from the broker's own image with its own command (see the README's Providers section).
"""
