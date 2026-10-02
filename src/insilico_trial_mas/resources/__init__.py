"""Packaged resources (population priors, report templates).

Kept as a real package so that ``importlib.resources`` works inside a Spark
executor, where the current working directory is not the project root.
"""
