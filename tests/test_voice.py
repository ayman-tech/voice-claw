from unittest import TestCase

from voice_claw.voice import SentenceBuffer


class VoiceTests(TestCase):
    def test_sentence_buffer_returns_complete_sentences(self) -> None:
        buffer = SentenceBuffer()

        self.assertEqual(buffer.push("Hello"), [])
        self.assertEqual(buffer.push(" world. Next"), ["Hello world."])
        self.assertEqual(buffer.push(" sentence!"), ["Next sentence!"])

    def test_sentence_buffer_flushes_tail_on_final(self) -> None:
        buffer = SentenceBuffer()

        self.assertEqual(buffer.push("No punctuation yet", final=True), ["No punctuation yet"])
        self.assertEqual(buffer.push("", final=True), [])

    def test_sentence_buffer_idle_flush_pattern(self) -> None:
        buffer = SentenceBuffer()

        self.assertEqual(buffer.push("Hello"), [])
        self.assertEqual(buffer.push("", final=True), ["Hello"])

    def test_sentence_buffer_clear_discards_pending_text(self) -> None:
        buffer = SentenceBuffer()

        buffer.push("Half spoken")
        buffer.clear()

        self.assertEqual(buffer.push("Fresh.", final=True), ["Fresh."])
