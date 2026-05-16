import queue
import unittest

from langgraph.errors import GraphInterrupt, Interrupt

from backend.agent import chat_graph


class ChatStreamInterruptTests(unittest.TestCase):
    def test_graph_interrupt_does_not_emit_error_event(self):
        def run_callable():
            raise GraphInterrupt((Interrupt(value={"type": "tool_approval"}, id="approval-1"),))
            yield None

        events = list(
            chat_graph._yield_stream_events(
                run_callable,
                queue.Queue(),
                "正在整理市场与账户数据",
            )
        )

        self.assertEqual(
            events,
            [{"type": "status", "stage": "preparing_context", "message": "正在整理市场与账户数据"}],
        )


if __name__ == "__main__":
    unittest.main()