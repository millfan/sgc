"""One module per pipeline stage. Each exposes a plain ``run`` function with typed inputs and outputs,
so a stage can be tested, replaced or rerun on its own. The LangGraph wiring lives in ``graph.py``."""
