"""Split-out FastAPI handler bodies for api.py.

Each module here holds the *bodies* of route handlers that used to live
inline in api.py.  api.py keeps the ``@app.get``/``@app.post`` decorators
and their ``Depends(require_admin_or_loopback)`` dependencies; the wrapper
functions there simply delegate to the callables in this package.

Handlers access api.py's module-level singletons (``memory``,
``line_monitor``, ``autonomous``) via a late ``from api import ...``
inside the function body to avoid a circular import at module load time.
"""
