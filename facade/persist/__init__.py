"""The DB-truth backend, split by responsibility.

``ModelPersistBackend`` composes these as mixins rather than delegating to collaborator objects,
and that is forced rather than preferred: the lease and reconcile clusters call each other in both
directions, five private methods are reached directly by tests, the singleton is constructed with
no arguments in dozens of places, and ``isinstance(persist_backend, PersistBackend)`` has to keep
passing. Mixins satisfy all four for free; delegation breaks every one.
"""
