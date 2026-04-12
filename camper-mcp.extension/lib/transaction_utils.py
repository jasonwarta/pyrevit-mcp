# -*- coding: utf-8 -*-
"""Transaction wrapper utilities for the camper-mcp pyRevit extension.

Provides a context-manager and helper for executing code inside a Revit
Transaction so that model-modifying operations are atomic and appear in
Revit's undo history with a meaningful name.
"""

import time
import traceback

from pyrevit.api import DB


class TransactionResult:
    """Holds the outcome of a transacted operation."""

    __slots__ = ("success", "result", "error", "traceback_str",
                 "execution_time_ms", "transaction_status")

    def __init__(self):
        self.success = False
        self.result = None
        self.error = None
        self.traceback_str = None
        self.execution_time_ms = 0
        self.transaction_status = "no_transaction"

    def to_dict(self):
        return {
            "success": self.success,
            "result": self.result,
            "error": self.error,
            "traceback": self.traceback_str,
            "execution_time_ms": self.execution_time_ms,
            "transaction_status": self.transaction_status,
        }


def run_in_transaction(doc, func, transaction_name="MCP Operation"):
    """Execute *func(doc)* inside a Revit Transaction.

    Returns a :class:`TransactionResult`.  On success the transaction is
    committed; on any exception it is rolled back.
    """
    tr = TransactionResult()
    txn = DB.Transaction(doc, transaction_name)
    start = time.time()
    try:
        txn.Start()
        tr.transaction_status = "started"
        tr.result = func(doc)
        status = txn.Commit()
        tr.transaction_status = str(status)
        tr.success = (str(status) == "Committed")
    except Exception as exc:
        tr.error = str(exc)
        tr.traceback_str = traceback.format_exc()
        try:
            if txn.HasStarted() and not txn.HasEnded():
                txn.RollBack()
                tr.transaction_status = "rolled_back"
        except Exception as rb_exc:
            tr.transaction_status = "rollback_failed"
            tr.error += " | Rollback failed: {}".format(rb_exc)
    finally:
        tr.execution_time_ms = int((time.time() - start) * 1000)
    return tr


def run_read_only(func, *args, **kwargs):
    """Execute *func* without a transaction (read-only operation).

    Returns a :class:`TransactionResult`.
    """
    tr = TransactionResult()
    start = time.time()
    try:
        tr.result = func(*args, **kwargs)
        tr.success = True
    except Exception as exc:
        tr.error = str(exc)
        tr.traceback_str = traceback.format_exc()
    finally:
        tr.execution_time_ms = int((time.time() - start) * 1000)
    return tr
