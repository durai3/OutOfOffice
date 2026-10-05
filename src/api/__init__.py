"""
HTTP/WebSocket glue for the DELEG8 web console (Sprint 6).

* :mod:`src.api.events`   - the live-feed hub that fans pipeline events out to WebSocket clients.
* :mod:`src.api.delegate` - dispatches and recalls the in-process meeting delegate.
* :mod:`src.api.routes`   - the ``/api/v1`` router the console talks to.

:mod:`src.main` owns app construction and includes the router.
"""
