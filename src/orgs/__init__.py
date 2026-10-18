"""Organizations: billing/seat/admin sits here, above the per-user tenancy.

Nothing in :mod:`src.tenancy` changes - every organization member still owns
their own :class:`~src.tenancy.workspace.Workspace`, exactly as a solo user
does. This package only adds identity, seats and admin structure on top.
"""
