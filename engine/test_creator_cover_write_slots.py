# -*- coding: utf-8 -*-
"""发布跳过封面路径与旧封面写入辅助函数的隔离回归；不访问平台。"""

import inspect
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import browser_cdp


def ready_slots(portrait=True, landscape=True):
    return {
        "portrait": {"ready": portrait, "images": []},
        "landscape": {"ready": landscape, "images": []},
    }


class CreatorCoverWriteSlotTests(unittest.TestCase):
    def test_publish_cover_selection_is_explicitly_skipped(self):
        progress = MagicMock()
        with patch.object(browser_cdp, "_ensure_creator_product_covers", side_effect=RuntimeError("cover writer failed")) as ensure:
            result = browser_cdp._creator_skip_product_covers(progress)

        ensure.assert_not_called()
        self.assertEqual(result["mode"], "skipped")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reasonCode"], "USER_GATE_PLATFORM_DEFAULT")
        self.assertTrue(result["platformDefaultRetained"])
        self.assertEqual(result["writtenSlots"], [])
        progress.assert_called_once()
        self.assertIn("跳过封面", progress.call_args.args[0]["stage"])

    def test_ai_fallback_writes_both_formal_slots_when_selection_is_confirmed(self):
        page = MagicMock()
        with patch.object(browser_cdp, "_creator_cover_frame_slots", side_effect=[
            ready_slots(False, False), ready_slots(), ready_slots(),
        ]), patch.object(browser_cdp, "_creator_wait_ai_cover_selection", return_value=(
            {"source": "https://cover.example/ai.jpg", "selectedBeforeClick": False, "selectedEvidence": "selected-class"}, ""
        )), patch.object(browser_cdp, "_creator_cover_assistant_status", return_value={
            "readable": True, "doubleCoverMissingVisible": False, "qualityWarnings": []
        }), patch.object(browser_cdp, "_report_operation_progress"):
            result, reason = browser_cdp._creator_try_ai_cover_frames(page)

        self.assertEqual(reason, "")
        self.assertEqual(result["mode"], "ai_recommend")
        self.assertEqual(result["slotReadback"], {"portrait": True, "landscape": True})
        self.assertEqual([frame["orientation"] for frame in result["frames"]], ["portrait", "landscape"])

    def test_partial_ai_slot_uses_editor_for_missing_ratio(self):
        page = MagicMock()
        with patch.object(browser_cdp, "_creator_cover_frame_slots", side_effect=[
            ready_slots(True, False), ready_slots(True, False), ready_slots(),
        ]), patch.object(browser_cdp, "_creator_wait_ai_cover_selection", return_value=(
            {"source": "https://cover.example/ai.jpg", "selectedBeforeClick": True, "selectedEvidence": "aria/data-selected"}, ""
        )), patch.object(browser_cdp, "_creator_save_selected_ai_cover_frame", return_value={
            "orientation": "landscape", "aspectRatio": "4:3", "saved": True,
            "source": "平台 AI 推荐封面",
        }) as save_frame, patch.object(browser_cdp, "_creator_cover_assistant_status", return_value={
            "readable": True, "doubleCoverMissingVisible": False, "qualityWarnings": []
        }), patch.object(browser_cdp, "_report_operation_progress"):
            result, reason = browser_cdp._creator_try_ai_cover_frames(page)

        self.assertEqual(reason, "")
        self.assertEqual(result["mode"], "ai_recommend")
        save_frame.assert_called_once_with(page, "landscape", "https://cover.example/ai.jpg", None)
        self.assertEqual(result["slotReadback"], {"portrait": True, "landscape": True})

    def test_product_detail_images_are_written_first_without_calling_ai(self):
        page = MagicMock()
        video = MagicMock()
        detail_check = {
            "status": "available", "reasonCode": "DETAIL_IMAGES_READY", "imageCount": 2,
            "selectedSource": "商品详情图/01.jpg", "coverFiles": {"portrait": "p", "landscape": "l"},
        }
        written = {"mode": "product_detail_images", "frames": [], "slotReadback": {"portrait": True, "landscape": True}}
        with patch.object(browser_cdp, "_creator_product_cover_assets", return_value=({"portrait": "p", "landscape": "l"}, detail_check)) as assets, \
                patch.object(browser_cdp, "_creator_set_both_cover_frames", return_value=written) as write_slots, \
                patch.object(browser_cdp, "_creator_try_ai_cover_frames") as try_ai, \
                patch.object(browser_cdp, "_report_operation_progress"):
            result = browser_cdp._ensure_creator_product_covers(page, video)

        assets.assert_called_once_with(video)
        write_slots.assert_called_once_with(page, {"portrait": "p", "landscape": "l"}, None)
        try_ai.assert_not_called()
        self.assertEqual(result["mode"], "product_detail_images")
        self.assertEqual(result["productDetailImageCheck"], detail_check)

    def test_ai_is_used_only_when_no_product_detail_images_are_available(self):
        page = MagicMock()
        video = MagicMock()
        unavailable = browser_cdp._CreatorProductDetailImagesUnavailable(
            Path("商品详情图"), "DETAIL_IMAGE_DIRECTORY_MISSING"
        )
        ai_result = {"mode": "ai_recommend", "slotReadback": {"portrait": True, "landscape": True}}
        with patch.object(browser_cdp, "_creator_product_cover_assets", side_effect=unavailable), \
                patch.object(browser_cdp, "_creator_try_ai_cover_frames", return_value=(ai_result, "")) as try_ai, \
                patch.object(browser_cdp, "_creator_set_both_cover_frames") as write_detail, \
                patch.object(browser_cdp, "_report_operation_progress"):
            result = browser_cdp._ensure_creator_product_covers(page, video)

        try_ai.assert_called_once_with(page, None)
        write_detail.assert_not_called()
        self.assertEqual(result["mode"], "ai_recommend")
        self.assertEqual(result["fallbackReason"], "NO_USABLE_PRODUCT_DETAIL_IMAGES")
        self.assertEqual(result["productDetailImageCheck"]["reasonCode"], "DETAIL_IMAGE_DIRECTORY_MISSING")

    def test_corrupt_or_unreadable_detail_image_does_not_fall_back_to_ai(self):
        page = MagicMock()
        video = MagicMock()
        with patch.object(browser_cdp, "_creator_product_cover_assets", side_effect=RuntimeError("详情图损坏")), \
                patch.object(browser_cdp, "_creator_try_ai_cover_frames") as try_ai, \
                patch.object(browser_cdp, "_report_operation_progress"):
            with self.assertRaisesRegex(RuntimeError, "避免误用 AI"):
                browser_cdp._ensure_creator_product_covers(page, video)

        try_ai.assert_not_called()

    def test_missing_detail_directory_is_explicitly_classified_for_ai_fallback(self):
        with TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "clip.mp4"
            with self.assertRaises(browser_cdp._CreatorProductDetailImagesUnavailable) as raised:
                browser_cdp._creator_product_cover_assets(video)

        self.assertEqual(raised.exception.reason_code, "DETAIL_IMAGE_DIRECTORY_MISSING")
        self.assertEqual(raised.exception.diagnostic()["imageCount"], 0)

    def test_empty_or_unsupported_detail_directory_has_observable_ai_fallback_reason(self):
        with TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "clip.mp4"
            detail_dir = Path(temp_dir) / "商品详情图"
            detail_dir.mkdir()
            (detail_dir / "readme.txt").write_text("no image", encoding="utf-8")
            with self.assertRaises(browser_cdp._CreatorProductDetailImagesUnavailable) as raised:
                browser_cdp._creator_product_cover_assets(video)

        diagnostic = raised.exception.diagnostic()
        self.assertEqual(diagnostic["reasonCode"], "NO_SUPPORTED_DETAIL_IMAGES")
        self.assertEqual(diagnostic["fileCount"], 1)
        self.assertEqual(diagnostic["ignoredFiles"], ["readme.txt"])

    def test_detail_image_path_that_is_a_file_is_not_classified_as_missing(self):
        with TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "clip.mp4"
            (Path(temp_dir) / "商品详情图").write_text("invalid path", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "不是目录") as raised:
                browser_cdp._creator_product_cover_assets(video)

        self.assertNotIsInstance(raised.exception, browser_cdp._CreatorProductDetailImagesUnavailable)

    def test_corrupt_detail_file_is_not_classified_as_missing_images(self):
        with TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "clip.mp4"
            detail_dir = Path(temp_dir) / "商品详情图"
            detail_dir.mkdir()
            (detail_dir / "01.jpg").write_bytes(b"not-an-image")
            with patch.object(browser_cdp, "resolve_ffmpeg", return_value="ffmpeg"), \
                    patch.object(browser_cdp, "build_product_cover", side_effect=RuntimeError("invalid image")):
                with self.assertRaisesRegex(RuntimeError, "不会被当作‘无图’") as raised:
                    browser_cdp._creator_product_cover_assets(video)

        self.assertNotIsInstance(raised.exception, browser_cdp._CreatorProductDetailImagesUnavailable)

    def test_usable_detail_image_returns_source_and_dual_cover_diagnostics(self):
        with TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "clip.mp4"
            detail_dir = Path(temp_dir) / "商品详情图"
            detail_dir.mkdir()
            source = detail_dir / "01.jpg"
            source.write_bytes(b"image")

            def build_cover(_ffmpeg, _source, output, _width, _height):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"x" * 1024)

            with patch.object(browser_cdp, "resolve_ffmpeg", return_value="ffmpeg"), \
                    patch.object(browser_cdp, "build_product_cover", side_effect=build_cover):
                assets, diagnostic = browser_cdp._creator_product_cover_assets(video)

        self.assertEqual(set(assets), {"portrait", "landscape"})
        self.assertEqual(diagnostic["status"], "available")
        self.assertEqual(diagnostic["imageCount"], 1)
        self.assertTrue(diagnostic["selectedSource"].endswith("01.jpg"))
        self.assertEqual(set(diagnostic["coverFiles"]), {"portrait", "landscape"})

    def test_detail_slot_write_failure_does_not_switch_to_ai(self):
        page = MagicMock()
        video = MagicMock()
        detail_check = {
            "status": "available", "reasonCode": "DETAIL_IMAGES_READY", "imageCount": 1,
            "selectedSource": "商品详情图/01.jpg", "coverFiles": {"portrait": "p", "landscape": "l"},
        }
        with patch.object(browser_cdp, "_creator_product_cover_assets", return_value=({"portrait": "p", "landscape": "l"}, detail_check)), \
                patch.object(browser_cdp, "_creator_set_both_cover_frames", side_effect=RuntimeError("竖封面探针未确认")), \
                patch.object(browser_cdp, "_creator_try_ai_cover_frames") as try_ai, \
                patch.object(browser_cdp, "_report_operation_progress"):
            with self.assertRaisesRegex(RuntimeError, "竖封面探针未确认"):
                browser_cdp._ensure_creator_product_covers(page, video)

        try_ai.assert_not_called()

    def test_video_upload_prepare_skips_cover_and_continues_to_cart(self):
        source = inspect.getsource(browser_cdp.prepare_douyin_publish)
        self.assertNotIn("_ensure_creator_product_covers", source)
        cover_index = source.index("cover_selection = _creator_skip_product_covers(progress)")
        stable_upload_index = source.index("if stable_ready_polls < 4:")
        cart_index = source.index("product_link_field = _select_creator_cart_tag")
        add_link_index = source.index("add_link_control.click")
        self.assertLess(stable_upload_index, cover_index)
        self.assertLess(cover_index, cart_index)
        self.assertLess(cover_index, add_link_index)

    def test_submit_skips_cover_recheck_before_final_publish(self):
        source = inspect.getsource(browser_cdp.submit_douyin_publish)
        self.assertNotIn("_ensure_creator_product_covers", source)
        cover_index = source.index("cover_selection = _creator_skip_product_covers(progress)")
        publish_index = source.index("_publish_creator_post(page, progress)")
        self.assertLess(cover_index, publish_index)

    def test_platform_cover_block_is_classified_before_publish_click(self):
        page = MagicMock()
        buttons = MagicMock()
        buttons.count.return_value = 1
        buttons.first.is_enabled.return_value = False
        page.get_by_role.return_value = buttons
        page.locator.return_value.inner_text.return_value = "横/竖双封面缺失，请设置封面"

        with self.assertRaises(browser_cdp.CreatorCoverRequirementBlocked) as raised:
            browser_cdp._publish_creator_post(page)

        self.assertEqual(raised.exception.classification, "CREATOR_COVER_REQUIRED_ISOLATED")
        self.assertFalse(raised.exception.submission_attempted)
        self.assertIn("未记录为发布成功", raised.exception.warning)
        buttons.first.click.assert_not_called()

    def test_cover_requirement_detector_requires_an_explicit_gate_phrase(self):
        for message in ("请先上传封面", "封面必填", "平台提示缺少封面"):
            with self.subTest(message=message):
                self.assertTrue(browser_cdp._creator_cover_requirement_blocked(message))
        self.assertFalse(browser_cdp._creator_cover_requirement_blocked("封面不佳，可更换画面"))

    def test_hidden_publish_button_with_cover_gate_is_classified(self):
        page = MagicMock()
        buttons = MagicMock()
        buttons.count.return_value = 0
        page.get_by_role.return_value = buttons
        page.locator.return_value.inner_text.return_value = "缺少封面，无法发布"

        with self.assertRaises(browser_cdp.CreatorCoverRequirementBlocked) as raised:
            browser_cdp._publish_creator_post(page)

        self.assertFalse(raised.exception.submission_attempted)

    def test_platform_cover_block_after_click_is_not_a_success_receipt(self):
        page = MagicMock()
        buttons = MagicMock()
        buttons.count.return_value = 1
        buttons.first.is_enabled.return_value = True
        page.get_by_role.side_effect = [buttons, MagicMock(count=MagicMock(return_value=0))]
        page.locator.return_value.inner_text.side_effect = ["投稿页", "请上传封面后再发布"]

        with self.assertRaises(browser_cdp.CreatorCoverRequirementBlocked) as raised:
            browser_cdp._publish_creator_post(page)

        self.assertEqual(raised.exception.classification, "CREATOR_COVER_REQUIRED_ISOLATED")
        self.assertTrue(raised.exception.submission_attempted)
        self.assertEqual(buttons.first.click.call_count, 1)

    def test_editor_probe_confirms_selected_portrait_and_matching_preview(self):
        state = browser_cdp._creator_cover_editor_state_from_probe({
            "editorVisible": True,
            "hasDone": True,
            "modeControls": {
                "portrait": {"present": True, "selected": True, "selectionKnown": True},
                "landscape": {"present": True, "selected": False, "selectionKnown": True},
            },
            "previewCandidates": [
                {"tag": "DIV", "className": "portrait-preview-frame", "width": 300, "height": 400,
                 "ratio": 0.75, "usableForAspect": True},
                {"tag": "VIDEO", "className": "source-video", "width": 640, "height": 360,
                 "ratio": 1.778, "usableForAspect": True},
            ],
            "domEvidence": {"root": {"text": "设置竖封面 设置横封面 完成"}},
        }, 0.75)

        self.assertTrue(state["editorVisible"])
        self.assertTrue(state["markers"]["hasPortraitMode"])
        self.assertFalse(state["markers"]["hasLandscapeMode"])
        self.assertEqual(state["previewAspect"]["ratio"], 0.75)
        self.assertEqual(len(state["previewCandidates"]), 2)

    def test_unselected_portrait_does_not_pass_on_landscape_or_empty_match(self):
        state = browser_cdp._creator_cover_editor_state_from_probe({
            "editorVisible": True,
            "hasDone": True,
            "modeControls": {
                "portrait": {"present": True, "selected": False, "selectionKnown": True},
                "landscape": {"present": True, "selected": True, "selectionKnown": True},
            },
            "previewCandidates": [
                {"tag": "VIDEO", "className": "source-video", "width": 640, "height": 360,
                 "ratio": 1.778, "usableForAspect": True},
                {"tag": "DIV", "className": "unknown-container", "width": 300, "height": 400,
                 "ratio": 0.75, "usableForAspect": False},
            ],
            "domEvidence": {"root": {"text": "设置竖封面 设置横封面 完成"}},
        }, 0.75)

        self.assertFalse(state["markers"]["hasPortraitMode"])
        self.assertTrue(state["markers"]["hasLandscapeMode"])
        self.assertIsNone(state["previewAspect"])
        self.assertIn(1.778, [item["ratio"] for item in state["previewCandidates"]])
        self.assertIn(0.75, [item["ratio"] for item in state["previewCandidates"]])

    def test_explicitly_inactive_portrait_is_not_rescued_by_stale_ratio_image(self):
        state = browser_cdp._creator_cover_editor_state_from_probe({
            "editorVisible": True,
            "hasDone": True,
            "modeControls": {
                "portrait": {"present": True, "selected": False, "selectionKnown": True},
                "landscape": {"present": True, "selected": False, "selectionKnown": True},
            },
            "previewCandidates": [
                {"tag": "IMG", "className": "cover-thumbnail", "width": 300, "height": 400,
                 "ratio": 0.75, "usableForAspect": True},
            ],
        }, 0.75)

        self.assertFalse(state["markers"]["hasPortraitMode"])
        self.assertIsNone(state["previewAspect"])

    def test_editor_tab_gets_one_force_retry_when_first_click_is_unconfirmed(self):
        page = MagicMock()
        card = MagicMock()
        preview_locator = MagicMock()
        preview_locator.count.return_value = 0
        card.locator.return_value = preview_locator
        not_confirmed = {
            "editorVisible": True,
            "markers": {"hasPortraitMode": False},
            "previewAspect": None,
        }
        confirmed = {
            "editorVisible": True,
            "markers": {"hasPortraitMode": True},
            "previewAspect": {"width": 300, "height": 400, "ratio": 0.75},
        }
        with patch.object(browser_cdp, "_creator_cover_frame_slots", return_value={
            "portrait": {"locator": card}
        }), patch.object(browser_cdp, "_visible_exact_text", return_value=[]), \
                patch.object(browser_cdp, "_creator_cover_editor_metrics", return_value={"editorVisible": True}), \
                patch.object(browser_cdp, "_creator_wait_for_cover_frame_confirmation", side_effect=[not_confirmed, confirmed]), \
                patch.object(browser_cdp, "_creator_click_cover_editor_text") as click_tab:
            config, preview_aspect = browser_cdp._creator_open_cover_frame_editor(page, "portrait")

        self.assertEqual(config["ratio"], "3:4")
        self.assertEqual(preview_aspect["ratio"], 0.75)
        self.assertEqual(click_tab.call_count, 2)
        self.assertEqual(click_tab.call_args_list[0].kwargs, {})
        self.assertEqual(click_tab.call_args_list[1].kwargs, {"force": True})

    def test_failed_editor_probe_writes_url_and_dom_evidence_without_query(self):
        page = MagicMock()
        page.url = "https://creator.douyin.com/creator-micro/content/post/video?token=private#cover"
        page.screenshot.side_effect = RuntimeError("screenshot unavailable")
        state = {
            "markers": {"hasPortraitMode": False},
            "previewCandidates": [{"tag": "VIDEO", "ratio": 1.778}],
            "domEvidence": {
                "pageTitle": "创作者中心",
                "readyState": "complete",
                "root": {"className": "cover-editor", "text": "设置竖封面 设置横封面 完成"},
            },
        }
        with TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"TOUGUANG_DATA_DIR": temp_dir}):
            path = browser_cdp._creator_write_cover_editor_diagnostic(page, "portrait", state, "预览未确认")
            payload = json.loads(Path(path).read_text(encoding="utf-8"))

        self.assertNotIn("token=private", payload["pageUrl"])
        self.assertNotIn("#cover", payload["pageUrl"])
        self.assertEqual(payload["readyState"], "complete")
        self.assertEqual(payload["editorState"]["previewCandidates"][0]["ratio"], 1.778)


if __name__ == "__main__":
    unittest.main()
