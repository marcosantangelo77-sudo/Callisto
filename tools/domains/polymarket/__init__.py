"""Polymarket domain — public prediction-market data, read-only.

See market.py for the adapter and resolver.py for outcome scoring. This
package contains NO wallet, NO keys, NO order path, and NO account
access: it reads public prices, computes edges, and records resolved
outcomes. That is the whole mandate.
"""
