# -*- coding: utf-8 -*-
"""候选卡片恢复时必须按商品 ID 匹配，不可依赖旧 cardIndex。"""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import browser_cdp
from browser_cdp import (
    BAIYING_ROW_METADATA_SCRIPT,
    _category_dropdown_for_item,
    _category_targets_for_root,
    _category_path_id,
    _click_category_and_confirm,
    _click_category_item,
    _click_filter_option,
    _filter_control,
    _hover_category_expand,
    _candidate_is_in_cart,
    _candidate_has_add_cart_action_text,
    _cart_response_outcome,
    _classify_baiying_prompt,
    _click_invoice_prompt_add_cart,
    _open_baiying_product_content_menu,
    _check_baiying_login,
    _dismiss_baiying_coupon_recommendation,
    _discard_creator_unpublished_video_draft,
    _visible_cart_error_message,
    _visible_baiying_prompt,
    _create_background_page,
    _extract_copied_product_url,
    _locate_candidate_card,
    _locate_baiying_product_row,
    _baiying_row_snapshot,
    _login_page_state,
    wait_for_baiying_login_interface,
    RecoverablePrioritySourceReadError,
    _collect_priority_source_ids,
    _read_priority_source_product_ids,
    _selection_error_requires_pause,
    _open_category_dropdown,
    _stabilize_background_filter_dropdown,
    apply_category_settings,
    apply_settings_and_collect_candidates,
    execute_selected_candidates,
    _category_query_settings,
    find_selection_page,
    remap_selected_candidate_indexes,
    run_selection_command,
    _product_url_matches_product,
    _select_baiying_product_row,
    CandidateCartSkip,
    selected_product_ids,
    _wait_category_item_checked,
    _wait_category_item_present,
)


class FakeCard:
    def __init__(self, html, visible=True, react_ids=()):
        self.html = html
        self.visible = visible
        self.react_ids = set(react_ids)

    def count(self):
        return 1

    def is_visible(self):
        return self.visible

    def evaluate(self, _script, expected_ids):
        return any(value in self.html or value in self.react_ids for value in expected_ids)


class FakeCardMatches:
    def __init__(self, cards):
        self.cards = cards

    def count(self):
        return len(self.cards)

    def nth(self, index):
        return self.cards[index]


class FakeTitleLocator:
    def __init__(self, card):
        self.card = card

    def locator(self, _selector):
        return FakeFirstLocator(self.card)


class FakeFirstLocator:
    def __init__(self, card):
        self.first = card


class FakeTitleMatches:
    def __init__(self, cards):
        self.cards = cards

    def count(self):
        return len(self.cards)

    def nth(self, index):
        return FakeTitleLocator(self.cards[index])


class FakeCategoryItem:
    def __init__(self, path_id):
        self.path_id = path_id

    def get_attribute(self, name):
        return self.path_id if name == "data-path-key" else None


class FakeCategoryItems:
    def __init__(self, path_ids):
        self.items = [FakeCategoryItem(path_id) for path_id in path_ids]

    @property
    def first(self):
        return self.items[0] if self.items else None

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeCategoryDropdown:
    def __init__(self, path_ids):
        self.path_ids = list(path_ids)

    def locator(self, selector):
        if 'data-path-key$="' in selector:
            value = selector.split('data-path-key$="', 1)[1].split('"', 1)[0]
            ids = [path_id for path_id in self.path_ids if path_id.endswith(value)]
        elif 'data-path-key="' in selector:
            value = selector.split('data-path-key="', 1)[1].split('"', 1)[0]
            ids = [path_id for path_id in self.path_ids if path_id == value]
        else:
            ids = self.path_ids
        return FakeCategoryItems(ids)


class FakeCategoryDropdowns:
    def __init__(self, dropdowns):
        self.dropdowns = dropdowns

    def count(self):
        return len(self.dropdowns)

    def nth(self, index):
        return self.dropdowns[index]


class FakeSearchInput:
    def __init__(self, value=""):
        self.value = value
        self.filled_values = []
        self.clear_click_count = 0

    def input_value(self, **_kwargs):
        return self.value

    def fill(self, value, **_kwargs):
        self.value = value
        self.filled_values.append(value)


class FakeSearchClearButton:
    def __init__(self, field):
        self.field = field

    def click(self, **_kwargs):
        self.field.value = ""
        self.field.clear_click_count += 1


class FakeSearchClearButtons:
    def __init__(self, field):
        self.first = FakeSearchClearButton(field)
        self.field = field

    def count(self):
        return 1 if self.field.value else 0


class FakeSearchInputs:
    def __init__(self, field):
        self.first = field

    def count(self):
        return 1


class FakePage:
    def __init__(self, title_to_cards, search_keyword=""):
        self.title_to_cards = title_to_cards
        self.listeners = {}
        self.search_input = FakeSearchInput(search_keyword)

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        if event in self.listeners and callback in self.listeners[event]:
            self.listeners[event].remove(callback)

    def wait_for_timeout(self, _milliseconds):
        return None

    def get_by_text(self, title, exact=True):
        assert exact is True
        return FakeTitleMatches(self.title_to_cards.get(title, []))

    def locator(self, selector):
        if selector == browser_cdp.SEARCH_KEYWORD_INPUT_SELECTOR:
            return FakeSearchInputs(self.search_input)
        if selector == browser_cdp.SEARCH_KEYWORD_CLEAR_SELECTOR:
            return FakeSearchClearButtons(self.search_input)
        assert selector == '[class*="__card___"]'
        cards = []
        for matches in self.title_to_cards.values():
            for card in matches:
                if card not in cards:
                    cards.append(card)
        return FakeCardMatches(cards)

    def evaluate(self, _script, *_args):
        return ""


class FakeResponse:
    url = "https://buyin.jinritemai.com/material_list"

    def __init__(self, payload, request_body=None, request=None, status=200, headers=None):
        self.payload = payload
        self.request = request or MagicMock()
        self.status = status
        self.headers = headers or {}
        if request is None or request_body is not None:
            self.request.post_data_json = request_body

    def json(self):
        return self.payload


class FakeMaterialRequest:
    url = "https://buyin.jinritemai.com/material_list"

    def __init__(self, category_id=None):
        self.post_data_json = (
            {"filters": [{"key": "MendelCid", "value": {"value": [category_id]}}]}
            if category_id else {}
        )
        self.failure = None


class BrowserCdpRecoveryTests(unittest.TestCase):
    def test_baiying_login_state_recognizes_workbench_navigation_without_filter_dom(self):
        body = MagicMock()
        body.inner_text.return_value = "首页 经营 选品 创作 营销 选品广场 热点趋势 联盟榜单"
        password_field = MagicMock()
        password_field.count.return_value = 0
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://buyin.jinritemai.com/dashboard/merch-picking-library"
        page.locator.side_effect = lambda selector: body if selector == "body" else password_field

        state = _login_page_state(page, "baiying")

        self.assertEqual(state["state"], "logged_in")
        self.assertIn("选品广场", state["message"])

    def test_baiying_login_state_requires_multiple_shell_markers_and_login_prompt_wins(self):
        body = MagicMock()
        password_field = MagicMock()
        password_field.count.return_value = 0
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://buyin.jinritemai.com/dashboard/merch-picking-library"
        page.locator.side_effect = lambda selector: body if selector == "body" else password_field

        body.inner_text.return_value = "选品广场"
        self.assertEqual(_login_page_state(page, "baiying")["state"], "unknown")

        body.inner_text.return_value = "首页 经营 选品广场 热点趋势 请先登录"
        self.assertEqual(_login_page_state(page, "baiying")["state"], "login_required")

        body.inner_text.return_value = "首页 经营 选品广场 热点趋势 安全验证"
        self.assertEqual(_login_page_state(page, "baiying")["state"], "verification_required")

    def test_baiying_login_check_waits_for_workbench_shell_after_navigation_commit(self):
        body = MagicMock()
        body.inner_text.side_effect = ["", "首页 经营 选品广场 热点趋势"]
        password_field = MagicMock()
        password_field.count.return_value = 0
        response = MagicMock()
        response.status = 200
        page = MagicMock()
        page.url = "https://buyin.jinritemai.com/dashboard/merch-picking-library"
        page.goto.return_value = response
        page.locator.side_effect = lambda selector: body if selector == "body" else password_field

        with patch.object(browser_cdp, "wait_for_baiying_login_interface", return_value=True) as wait_shell:
            state = _check_baiying_login(page)

        self.assertEqual(state["state"], "logged_in")
        page.goto.assert_called_once_with(
            "https://buyin.jinritemai.com/dashboard/merch-picking-library",
            wait_until="commit",
            timeout=12_000,
        )
        wait_shell.assert_called_once_with(page, timeout_ms=5_000)

    def test_baiying_login_interface_wait_uses_shell_and_filter_markers(self):
        page = MagicMock()

        self.assertTrue(wait_for_baiying_login_interface(page))

        wait_call = page.wait_for_function.call_args
        self.assertIn("shellMatches.length >= 2", wait_call.args[0])
        self.assertIn("merch-filter-cate-cascader-item", wait_call.args[0])
        self.assertEqual(wait_call.kwargs["timeout"], 15_000)
        self.assertIn("选品广场", wait_call.kwargs["arg"]["markers"])

    def test_priority_source_waits_for_commit_and_keeps_page_for_timeout_snapshot(self):
        source_url = "https://buyin.jinritemai.com/dashboard/merch-picking-hall/rank"
        source_page = MagicMock()
        source_page.url = source_url
        response = MagicMock()
        response.status = 200
        source_page.goto.return_value = response
        source_page.wait_for_function.side_effect = browser_cdp.PlaywrightTimeoutError("readiness timeout")
        source_page.evaluate.return_value = {
            "ids": [], "visibleRowCount": 3, "emptyState": False,
            "pageTitle": "Alliance rank", "pagePath": "/dashboard/merch-picking-hall/rank",
            "rootClassSamples": ["aurora-table-row"], "classHints": ["product-item"],
        }
        context = MagicMock()
        context.new_page.return_value = source_page
        page = MagicMock()
        page.context = context

        with patch.object(browser_cdp, "_visible_baiying_prompt", return_value=("", "")) as prompt_check:
            with self.assertRaises(RecoverablePrioritySourceReadError):
                _read_priority_source_product_ids(page, "allianceRank")

        source_page.goto.assert_called_once_with(source_url, wait_until="commit", timeout=45_000)
        self.assertEqual(prompt_check.call_count, 2)
        call_names = [call[0] for call in source_page.mock_calls]
        self.assertLess(call_names.index("wait_for_function"), call_names.index("evaluate"))
        self.assertLess(call_names.index("evaluate"), call_names.index("close"))

    def test_unreadable_optional_priority_source_is_skipped_and_next_source_continues(self):
        page = MagicMock()

        def read_source(_page, source_key):
            if source_key == "allianceRank":
                raise RecoverablePrioritySourceReadError("榜单没有可识别的商品列表")
            return {"123456789"}

        settings = {"sources": {"priority": ["allianceRank", "hotSpot"]}}
        with patch.object(browser_cdp, "_read_priority_source_product_ids", side_effect=read_source) as read:
            membership, warnings, results = _collect_priority_source_ids(page, settings)

        self.assertEqual(read.call_count, 2)
        self.assertEqual(membership["allianceRank"], set())
        self.assertEqual(membership["hotSpot"], {"123456789"})
        self.assertEqual([result["status"] for result in results], ["skipped", "read"])
        self.assertTrue(any("继续其他来源和选品广场" in warning for warning in warnings))

    def test_playwright_navigation_timeout_still_requires_account_pause(self):
        error = browser_cdp.PlaywrightTimeoutError("Page.goto: Timeout 45000ms exceeded")
        self.assertTrue(_selection_error_requires_pause(error))

    def test_creator_unpublished_draft_is_discarded_once_before_fresh_upload(self):
        prompt = "你还有上次未发布的视频，是否继续编辑？"
        body = MagicMock()
        body.inner_text.side_effect = [prompt, "发布视频"]
        page = MagicMock()
        page.url = "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page"
        page.locator.return_value = body

        abandon_button = MagicMock()
        abandon_button.is_visible.return_value = True
        abandon_button.is_enabled.return_value = True
        abandon_buttons = MagicMock()
        abandon_buttons.count.return_value = 1
        abandon_buttons.first = abandon_button

        continue_button = MagicMock()
        continue_button.is_visible.return_value = True
        continue_buttons = MagicMock()
        continue_buttons.count.return_value = 1
        continue_buttons.first = continue_button
        page.get_by_role.side_effect = lambda _role, name, exact: {
            "放弃编辑": abandon_buttons,
            "继续编辑": continue_buttons,
        }[name]

        self.assertTrue(_discard_creator_unpublished_video_draft(page))
        abandon_button.click.assert_called_once_with(timeout=5_000)
        continue_button.click.assert_not_called()

    def test_creator_draft_with_ambiguous_buttons_stops_without_clicking(self):
        body = MagicMock()
        body.inner_text.return_value = "你还有上次未发布的视频，是否继续编辑？"
        page = MagicMock()
        page.locator.return_value = body
        abandon_buttons = MagicMock()
        abandon_buttons.count.return_value = 2
        continue_buttons = MagicMock()
        continue_buttons.count.return_value = 1
        page.get_by_role.side_effect = lambda _role, name, exact: {
            "放弃编辑": abandon_buttons,
            "继续编辑": continue_buttons,
        }[name]

        with self.assertRaisesRegex(RuntimeError, "按钮不唯一"):
            _discard_creator_unpublished_video_draft(page)
        abandon_buttons.first.click.assert_not_called()
        continue_buttons.first.click.assert_not_called()

    def test_manual_retry_never_discards_existing_draft_before_new_upload(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = "你还有上次未发布的视频，是否继续编辑？"
        with self.assertRaisesRegex(RuntimeError, "手动重发未放弃草稿"):
            _discard_creator_unpublished_video_draft(page, preserve_existing=True)
        page.get_by_role.assert_not_called()

    def test_creator_without_unpublished_draft_is_left_untouched(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = "发布视频"
        self.assertFalse(_discard_creator_unpublished_video_draft(page))
        page.get_by_role.assert_not_called()

    def test_static_creator_upload_tooltip_does_not_look_like_active_upload(self):
        self.assertFalse(browser_cdp._creator_upload_in_progress(
            "点击发布后，如作品还在上传中，请勿关闭页面，等待上传发布完成。 发布设置 立即发布"
        ))
        self.assertTrue(browser_cdp._creator_upload_in_progress("视频上传中 45%"))
        self.assertTrue(browser_cdp._creator_upload_in_progress("视频校验中"))

    def test_description_readback_ignores_only_trailing_zero_width_cursor_marker(self):
        title = "商品名称"
        self.assertEqual(browser_cdp.normalize_creator_description_readback(title + "\u200b"), title)
        self.assertEqual(browser_cdp.normalize_creator_description_readback(title), title)
        self.assertNotEqual(browser_cdp.normalize_creator_description_readback(title + "X\u200b"), title)

    def test_creator_post_route_matches_live_creator_center_page(self):
        self.assertTrue(browser_cdp._is_creator_publish_page(
            "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page"
        ))
        self.assertTrue(browser_cdp._is_creator_publish_page(
            "https://creator.douyin.com/creator-micro/content/post/video/"
        ))
        self.assertFalse(browser_cdp._is_creator_publish_page(
            "https://creator.douyin.com/creator-micro/content/upload"
        ))
        self.assertFalse(browser_cdp._is_creator_publish_page(
            "https://example.com/creator-micro/content/post/video"
        ))

    def test_creator_page_reuses_existing_post_video_page(self):
        class ExistingCreatorPage:
            url = "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page"
            brought_to_front = False

            def is_closed(self):
                return False

            def bring_to_front(self):
                self.brought_to_front = True

        class ExistingCreatorContext:
            def __init__(self, page):
                self.pages = [page]

            def new_page(self):
                raise AssertionError("现有投稿页应被复用，不应新开错误地址")

        page = ExistingCreatorPage()
        result = browser_cdp._creator_page(ExistingCreatorContext(page))
        self.assertIs(result, page)
        self.assertTrue(page.brought_to_front)

    def test_baiying_creator_action_accepts_semantic_label_in_copy_dialog(self):
        self.assertTrue(browser_cdp._is_baiying_creator_action_label("立即 创作"))
        self.assertTrue(browser_cdp._is_baiying_creator_action_label("添品并创作"))
        self.assertFalse(browser_cdp._is_baiying_creator_action_label("知道啦"))

        create = MagicMock()
        create.is_visible.return_value = True
        create.is_enabled.return_value = True
        create.evaluate.return_value = ["立即创作"]
        dismiss = MagicMock()
        dismiss.is_visible.return_value = True
        dismiss.is_enabled.return_value = True
        dismiss.evaluate.return_value = ["知道啦"]
        dialog = MagicMock()
        dialog.query_selector_all.side_effect = lambda selector: (
            [create, dismiss] if selector.startswith("button,") else []
        )
        self.assertEqual(browser_cdp._baiying_dialog_creator_actions(dialog), [create])

    def test_baiying_creator_missing_or_ambiguous_action_uses_direct_url_without_click(self):
        cases = ((None, []), (MagicMock(), []), (MagicMock(), [MagicMock(), MagicMock()]))
        for dialog, actions in cases:
            with self.subTest(dialog_visible=dialog is not None, action_count=len(actions)):
                cart_page = MagicMock()
                cart_page.url = "https://buyin.jinritemai.com/dashboard/merch-picking-cart"
                cart_page.is_closed.return_value = False
                context = MagicMock()
                context.pages = [cart_page]
                direct_page = MagicMock()
                with (
                    patch.object(browser_cdp, "_visible_baiying_copy_dialog", return_value=dialog),
                    patch.object(browser_cdp, "_baiying_dialog_creator_actions", return_value=actions),
                    patch.object(browser_cdp, "_open_creator_directly", return_value=direct_page) as direct,
                    patch.object(browser_cdp.time, "monotonic", side_effect=[0, 1, 9]),
                ):
                    result = browser_cdp._open_creator_from_baiying(context, cart_page)
                self.assertIs(result, direct_page)
                direct.assert_called_once_with(context)
                for action in actions:
                    action.click.assert_not_called()

    def test_baiying_creator_jump_does_not_accept_preexisting_post_page(self):
        cart_page = MagicMock()
        cart_page.url = "https://buyin.jinritemai.com/dashboard/merch-picking-cart"
        cart_page.is_closed.return_value = False
        marker = MagicMock()
        marker.is_visible.return_value = True
        markers = MagicMock()
        markers.count.return_value = 1
        markers.nth.return_value = marker
        cart_page.get_by_text.return_value = markers
        old_post_page = MagicMock()
        old_post_page.url = "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page"
        old_post_page.is_closed.return_value = False
        context = MagicMock()
        context.pages = [cart_page, old_post_page]
        create = MagicMock()
        direct_page = MagicMock()
        with (
            patch.object(browser_cdp, "_visible_baiying_copy_dialog", return_value=MagicMock()),
            patch.object(browser_cdp, "_baiying_dialog_creator_actions", return_value=[create]),
            patch.object(browser_cdp, "_open_creator_directly", return_value=direct_page) as direct,
            patch.object(browser_cdp.time, "monotonic", side_effect=[0, 1, 2, 3, 33]),
        ):
            result = browser_cdp._open_creator_from_baiying(context, cart_page)
        self.assertIs(result, direct_page)
        create.click.assert_called_once_with(timeout=5_000)
        old_post_page.bring_to_front.assert_not_called()
        direct.assert_called_once_with(context)

    def test_baiying_creator_jump_page_changed_without_post_page_stops_without_direct_retry(self):
        cart_page = MagicMock()
        cart_page.url = "https://buyin.jinritemai.com/dashboard/merch-picking-cart"
        cart_page.is_closed.return_value = False
        context = MagicMock()
        context.pages = [cart_page]
        create = MagicMock()
        create.click.side_effect = lambda **_: setattr(
            cart_page, "url", "https://buyin.jinritemai.com/dashboard/unknown-transition"
        )
        with (
            patch.object(browser_cdp, "_visible_baiying_copy_dialog", return_value=MagicMock()),
            patch.object(browser_cdp, "_baiying_dialog_creator_actions", return_value=[create]),
            patch.object(browser_cdp, "_open_creator_directly") as direct,
            patch.object(browser_cdp.time, "monotonic", side_effect=[0, 1, 2, 3, 33]),
        ):
            with self.assertRaisesRegex(RuntimeError, "页面状态已变化"):
                browser_cdp._open_creator_from_baiying(context, cart_page)
        create.click.assert_called_once_with(timeout=5_000)
        direct.assert_not_called()

    def test_baiying_creator_jump_reuses_only_new_creator_home_page(self):
        cart_page = MagicMock()
        cart_page.url = "https://buyin.jinritemai.com/dashboard/merch-picking-cart"
        cart_page.is_closed.return_value = False
        markers = MagicMock()
        markers.count.return_value = 0
        cart_page.get_by_text.return_value = markers
        old_post_page = MagicMock()
        old_post_page.url = "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page"
        old_post_page.is_closed.return_value = False
        new_home_page = MagicMock()
        new_home_page.url = "https://creator.douyin.com/creator-micro/home"
        new_home_page.is_closed.return_value = False
        context = MagicMock()
        context.pages = [cart_page, old_post_page]
        create = MagicMock()
        create.click.side_effect = lambda **_: context.pages.append(new_home_page)
        with (
            patch.object(browser_cdp, "_visible_baiying_copy_dialog", return_value=MagicMock()),
            patch.object(browser_cdp, "_baiying_dialog_creator_actions", return_value=[create]),
            patch.object(browser_cdp, "_open_creator_directly", return_value=new_home_page) as direct,
            patch.object(browser_cdp.time, "monotonic", side_effect=[0, 1, 2, 3, 33]),
        ):
            result = browser_cdp._open_creator_from_baiying(context, cart_page)
        self.assertIs(result, new_home_page)
        create.click.assert_called_once_with(timeout=5_000)
        direct.assert_called_once_with(context, new_home_page)
        old_post_page.bring_to_front.assert_not_called()

    def test_copied_buyin_product_link_must_match_product_id_and_trusted_host(self):
        product_id = "3836151927951982908"
        valid = f"https://haohuo.jinritemai.com/ecommerce/trade/detail?product_id={product_id}"
        self.assertTrue(_product_url_matches_product(valid, product_id))
        self.assertFalse(_product_url_matches_product(
            "https://haohuo.jinritemai.com/ecommerce/trade/detail?product_id=3836156624599384510",
            product_id,
        ))
        self.assertFalse(_product_url_matches_product(
            f"https://example.com/ecommerce/trade/detail?id={product_id}",
            product_id,
        ))

    def test_copied_product_link_is_extracted_from_confirmation_text(self):
        product_id = "3836151927951982908"
        url = f"https://haohuo.jinritemai.com/ecommerce/trade/detail?id={product_id}"
        copied = _extract_copied_product_url(f"已成功复制商品链接：{url}。", product_id)
        self.assertEqual(copied, url)

    def test_copied_product_link_without_matching_id_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "商品 ID"):
            _extract_copied_product_url(
                "https://haohuo.jinritemai.com/views/product/item2?id=wrong-product",
                "3836151927951982908",
            )

    def test_material_response_must_match_requested_category_id(self):
        settings = {"categorySelection": {
            "level1": ["root"], "level2": ["root__RC_CASCADER_SPLIT__second"],
            "level3": ["root__RC_CASCADER_SPLIT__second__RC_CASCADER_SPLIT__leaf"],
        }}
        matching = FakeResponse({}, {"filters": [{"key": "MendelCid", "value": {"value": ["leaf"]}}]})
        other_category = FakeResponse({}, {"filters": [{"key": "MendelCid", "value": {"value": ["other"]}}]})
        missing_category = FakeResponse({}, {"filters": [{"key": "price", "value": {"value": ["100"]}}]})
        raw_body_only = FakeResponse({}, None)
        raw_body_only.request.post_data_json = None
        raw_body_only.request.post_data = '{"filters":[{"key":"MendelCid","value":{"value":["leaf"]}}]}'

        self.assertTrue(browser_cdp._response_matches_category(matching, settings))
        self.assertTrue(browser_cdp._response_matches_category(raw_body_only, settings))
        self.assertFalse(browser_cdp._response_matches_category(other_category, settings))
        self.assertFalse(browser_cdp._response_matches_category(missing_category, settings))

    def test_recent_category_result_is_reused_without_reapplying_filters(self):
        page = FakePage({})
        settings = {
            "candidateLimit": 1,
            "categorySelection": {"level1": ["root"], "level2": ["second"], "level3": ["leaf"]},
        }
        signature = browser_cdp._selection_filter_signature(settings)
        payload = {"data": {"summary_promotions": [{
            "product_id": "cached-product",
            "base_model": {"product_info": {"name": "缓存候选"}},
        }]}}
        key = (id(page), signature)
        browser_cdp._MATERIAL_LIST_CACHE[key] = (browser_cdp.time.monotonic(), payload)
        try:
            with patch.object(browser_cdp, "apply_filter_settings") as apply, \
                    patch.object(browser_cdp, "search_baiying_candidates") as search:
                result = browser_cdp._apply_one_category_and_collect_candidates(page, settings)

            apply.assert_not_called()
            search.assert_not_called()
            self.assertEqual([row["productId"] for row in result["candidates"]], ["cached-product"])
        finally:
            browser_cdp._MATERIAL_LIST_CACHE.pop(key, None)

    def test_expired_category_result_is_not_reused(self):
        key = (987654321, "expired-signature")
        browser_cdp._MATERIAL_LIST_CACHE[key] = (
            browser_cdp.time.monotonic() - browser_cdp.MATERIAL_LIST_CACHE_TTL_SECONDS - 1,
            {"expired": True},
        )
        self.assertIsNone(browser_cdp._get_cached_material_list(*key))
        self.assertNotIn(key, browser_cdp._MATERIAL_LIST_CACHE)

    def test_non_category_filters_can_only_be_reused_when_identical(self):
        previous = {
            "categorySelection": {"level1": ["root"], "level2": ["second"], "level3": ["old-leaf"]},
            "filters": {"带货情况": {"月销": "≥500"}},
        }
        same_filters = {
            "categorySelection": {"level1": ["root"], "level2": ["second"], "level3": ["new-leaf"]},
            "filters": {"带货情况": {"月销": "≥500"}},
        }
        changed_filters = {
            **same_filters,
            "filters": {"带货情况": {"月销": "不限"}},
        }

        self.assertTrue(browser_cdp._can_reuse_applied_non_category_filters(
            browser_cdp._selection_filter_signature(previous), same_filters
        ))
        self.assertFalse(browser_cdp._can_reuse_applied_non_category_filters(
            browser_cdp._selection_filter_signature(previous), changed_filters
        ))

    def test_search_keyword_is_part_of_applied_and_cache_signatures(self):
        settings = {
            "categorySelection": {"level1": ["root"]},
            "filters": {"带货情况": {"月销": "≥500"}},
        }
        whitespace_keyword = {**settings, "searchKeyword": "  "}
        explicit_keyword = {**settings, "searchKeyword": "英语口语"}

        self.assertEqual(
            browser_cdp._selection_filter_signature(settings),
            browser_cdp._selection_filter_signature(whitespace_keyword),
        )
        self.assertNotEqual(
            browser_cdp._selection_filter_signature(settings),
            browser_cdp._selection_filter_signature(explicit_keyword),
        )
        self.assertFalse(browser_cdp._can_reuse_applied_non_category_filters(
            browser_cdp._selection_filter_signature(explicit_keyword), settings
        ))

    def test_search_keyword_sync_clears_unconfigured_value_and_keeps_explicit_value(self):
        page = FakePage({}, search_keyword="英语口语")

        self.assertTrue(browser_cdp._apply_search_keyword(page, {}))
        self.assertEqual(page.search_input.value, "")
        self.assertEqual(page.search_input.clear_click_count, 1)
        self.assertEqual(page.search_input.filled_values, [])
        self.assertFalse(browser_cdp._apply_search_keyword(page, {}))

        page.search_input.value = "英语口语"
        self.assertFalse(browser_cdp._apply_search_keyword(page, {"searchKeyword": "英语口语"}))
        self.assertEqual(page.search_input.value, "英语口语")
        self.assertEqual(page.search_input.clear_click_count, 1)

    def test_search_keyword_sync_rejects_switching_to_another_nonempty_keyword(self):
        page = FakePage({}, search_keyword="旧关键词")

        with self.assertRaisesRegex(RuntimeError, "不能安全切换非空关键词.*已停止查询"):
            browser_cdp._apply_search_keyword(page, {"searchKeyword": "新关键词"})

        self.assertEqual(page.search_input.value, "旧关键词")
        self.assertEqual(page.search_input.clear_click_count, 0)
        self.assertEqual(page.search_input.filled_values, [])

    def test_search_keyword_sync_fails_closed_when_page_restores_stale_value(self):
        page = FakePage({}, search_keyword="英语口语")
        page.wait_for_timeout = lambda _milliseconds: setattr(page.search_input, "value", "页面回写旧词")

        with self.assertRaisesRegex(RuntimeError, "页面更新后与当前设置不一致.*已停止查询"):
            browser_cdp._apply_search_keyword(page, {})

        self.assertEqual(page.search_input.clear_click_count, 1)
        self.assertEqual(page.search_input.value, "页面回写旧词")

    def test_preserve_other_filters_mode_only_changes_category(self):
        page = MagicMock()
        settings = {
            "categorySelection": {"level1": ["root"], "level2": ["second"], "level3": ["leaf"]},
            "categoryTree": [{"id": "root", "children": []}],
            "filters": {"带货情况": {"月销": "≥500"}},
        }
        with patch.object(browser_cdp, "apply_category_settings", return_value=["类目已切换"]) as apply_category:
            warnings = browser_cdp.apply_filter_settings(page, settings, preserve_other_filters=True)

        apply_category.assert_called_once_with(page, settings)
        page.get_by_text.assert_not_called()
        page.wait_for_timeout.assert_not_called()
        self.assertEqual(warnings, ["类目已切换"])

    def test_platform_error_11001_stops_without_retry(self):
        page = FakePage({})
        search = MagicMock()

        def emit_network_error(**_kwargs):
            response = FakeResponse(
                {"code": 11001, "msg": "当前网络不稳定"},
                {"filters": [{"key": "MendelCid", "value": {"value": ["leaf"]}}]},
            )
            for callback in list(page.listeners.get("response", [])):
                callback(response)

        search.click.side_effect = emit_network_error
        page.get_by_text = MagicMock(return_value=MagicMock(first=search))
        page.wait_for_timeout = MagicMock()
        settings = {"categorySelection": {"level1": ["root"], "level2": ["second"], "level3": ["leaf"]}}

        with patch.object(browser_cdp, "_mark_page_rate_limited") as mark_rate_limited:
            with self.assertRaisesRegex(RuntimeError, "material_list 接口返回业务错误 11001"):
                browser_cdp.search_baiying_candidates(page, settings)

        self.assertEqual(search.click.call_count, 1)
        self.assertNotIn(8_000, [call.args[0] for call in page.wait_for_timeout.call_args_list])
        mark_rate_limited.assert_called_once_with(id(page))

    def test_11001_diagnostics_keep_only_sanitized_response_fields(self):
        collector = browser_cdp._MaterialListCollector({})
        response = FakeResponse(
            {"code": 11001, "msg": "当前网络不稳定 https://example.test/path?keyword=private Cookie: session=secret"},
            status=200,
            headers={
                "x-tt-logid": "trace-12345",
                "set-cookie": "session=header-secret",
            },
        )

        collector.on_response(response)
        error = collector.latest_error_since(0)

        self.assertIsNotNone(error)
        detail = error[2]
        self.assertIn("HTTP 200", detail)
        self.assertIn("响应业务码 11001", detail)
        self.assertIn("百应响应说明：当前网络不稳定", detail)
        self.assertIn("请求追踪号：trace-12345", detail)
        self.assertIn("参数已隐藏", detail)
        self.assertNotIn("private", detail)
        self.assertNotIn("session=secret", detail)
        self.assertNotIn("header-secret", detail)

    def test_direct_search_waits_for_pending_requests_and_chooses_latest_started(self):
        page = FakePage({})
        search = MagicMock()
        search.count.return_value = 1
        page.get_by_text = MagicMock(return_value=MagicMock(first=search))
        older_request = FakeMaterialRequest()
        newer_request = FakeMaterialRequest()
        older_payload = {"data": {"summary_promotions": [{
            "product_id": "older-request-product",
            "base_model": {"product_info": {"name": "较早发起但较晚返回"}},
        }]}}
        newer_payload = {"data": {"summary_promotions": [{
            "product_id": "newer-request-product",
            "base_model": {"product_info": {"name": "较晚发起但较早返回"}},
        }]}}
        clock = [100.0]
        waits = [0]

        def emit(event, value):
            for callback in list(page.listeners.get(event, [])):
                callback(value)

        def click(**_kwargs):
            emit("request", older_request)
            emit("request", newer_request)
            emit("response", FakeResponse(newer_payload, request=newer_request))
            emit("requestfinished", newer_request)

        def wait_for_timeout(_milliseconds):
            waits[0] += 1
            clock[0] += 0.2
            if waits[0] == 9:
                emit("response", FakeResponse(older_payload, request=older_request))
                emit("requestfinished", older_request)

        search.click.side_effect = click
        page.wait_for_timeout = wait_for_timeout

        with patch.object(browser_cdp.time, "time", side_effect=lambda: clock[0]):
            result = browser_cdp.search_baiying_candidates(page, {})

        self.assertGreaterEqual(waits[0], 9)
        self.assertEqual(
            [row["productId"] for row in result["candidates"]],
            ["newer-request-product"],
        )
        self.assertEqual(search.click.call_count, 1)
        self.assertFalse(page.listeners.get("request"))
        self.assertFalse(page.listeners.get("requestfinished"))

    def test_late_11001_overrides_success_and_marks_cooldown(self):
        page = FakePage({})
        search = MagicMock()
        search.count.return_value = 1
        page.get_by_text = MagicMock(return_value=MagicMock(first=search))
        older_request = FakeMaterialRequest()
        newer_request = FakeMaterialRequest()
        success = {"data": {"summary_promotions": [{
            "product_id": "must-not-be-consumed",
            "base_model": {"product_info": {"name": "并行成功"}},
        }]}}
        clock = [100.0]
        waits = [0]

        def emit(event, value):
            for callback in list(page.listeners.get(event, [])):
                callback(value)

        def click(**_kwargs):
            emit("request", older_request)
            emit("request", newer_request)
            emit("response", FakeResponse(success, request=newer_request))
            emit("requestfinished", newer_request)

        def wait_for_timeout(_milliseconds):
            waits[0] += 1
            clock[0] += 0.2
            if waits[0] == 2:
                emit("response", FakeResponse(
                    {"code": 11001, "msg": "当前网络不稳定"}, request=older_request
                ))
                emit("requestfinished", older_request)

        search.click.side_effect = click
        page.wait_for_timeout = wait_for_timeout

        with patch.object(browser_cdp.time, "time", side_effect=lambda: clock[0]), \
                patch.object(browser_cdp, "_mark_page_rate_limited") as mark_rate_limited:
            with self.assertRaisesRegex(RuntimeError, "返回业务错误 11001.*百应响应说明：当前网络不稳定"):
                browser_cdp.search_baiying_candidates(page, {})

        self.assertEqual(search.click.call_count, 1)
        mark_rate_limited.assert_called_once_with(id(page))

    def test_category_filter_waits_for_pending_requests_and_uses_latest_started(self):
        page = FakePage({})
        settings = {
            "candidateLimit": 1,
            "categorySelection": {
                "level1": ["root"],
                "level2": ["second"],
                "level3": ["leaf"],
            },
        }
        older_request = FakeMaterialRequest("leaf")
        newer_request = FakeMaterialRequest("leaf")
        older_payload = {"data": {"summary_promotions": [{
            "product_id": "older-filter-product",
            "base_model": {"product_info": {"name": "较早筛选请求"}},
        }]}}
        newer_payload = {"data": {"summary_promotions": [{
            "product_id": "newer-filter-product",
            "base_model": {"product_info": {"name": "较晚筛选请求"}},
        }]}}
        clock = [100.0]
        waits = [0]

        def emit(event, value):
            for callback in list(page.listeners.get(event, [])):
                callback(value)

        def apply_and_dispatch(_page, _settings):
            emit("request", older_request)
            emit("request", newer_request)
            emit("response", FakeResponse(newer_payload, request=newer_request))
            emit("requestfinished", newer_request)
            return []

        def wait_for_timeout(_milliseconds):
            waits[0] += 1
            clock[0] += 0.2
            if waits[0] == 9:
                emit("response", FakeResponse(older_payload, request=older_request))
                emit("requestfinished", older_request)

        page.wait_for_timeout = wait_for_timeout
        with patch.object(browser_cdp.time, "time", side_effect=lambda: clock[0]), \
                patch.object(browser_cdp, "_get_cached_material_list", return_value=None), \
                patch.object(browser_cdp, "apply_filter_settings", side_effect=apply_and_dispatch), \
                patch.object(browser_cdp, "search_baiying_candidates") as search:
            result = browser_cdp._apply_one_category_and_collect_candidates(
                page, settings, force_refresh=True
            )

        self.assertGreaterEqual(waits[0], 9)
        self.assertEqual(
            [row["productId"] for row in result["candidates"]],
            ["newer-filter-product"],
        )
        search.assert_not_called()

    def test_direct_search_clears_stale_keyword_before_click(self):
        page = FakePage({}, search_keyword="英语口语")
        search = MagicMock()
        search.count.return_value = 1
        page.get_by_text = MagicMock(return_value=MagicMock(first=search))
        page.wait_for_timeout = MagicMock()
        payload = {"data": {"summary_promotions": [{
            "product_id": "keyword-cleared-product",
            "base_model": {"product_info": {"name": "清除旧关键词后商品"}},
        }]}}

        def emit_success(**_kwargs):
            self.assertEqual(page.search_input.value, "")
            response = FakeResponse(payload, {})
            for callback in list(page.listeners.get("response", [])):
                callback(response)

        search.click.side_effect = emit_success
        clock = [0.0]

        def tick():
            clock[0] += 2.0
            return clock[0]

        with patch.object(browser_cdp.time, "time", side_effect=tick):
            result = browser_cdp.search_baiying_candidates(page, {})

        self.assertEqual(page.search_input.clear_click_count, 1)
        self.assertEqual([row["productId"] for row in result["candidates"]], ["keyword-cleared-product"])
        search.click.assert_called_once()

    def test_explicit_platform_business_error_is_not_retried(self):
        page = FakePage({})
        search = MagicMock()

        def emit_business_error(**_kwargs):
            response = FakeResponse(
                {"code": 4001, "msg": "筛选条件无效"},
                {"filters": [{"key": "MendelCid", "value": {"value": ["leaf"]}}]},
            )
            for callback in list(page.listeners.get("response", [])):
                callback(response)

        search.click.side_effect = emit_business_error
        page.get_by_text = MagicMock(return_value=MagicMock(first=search))
        page.wait_for_timeout = MagicMock()
        settings = {"categorySelection": {"level1": ["root"], "level2": ["second"], "level3": ["leaf"]}}

        with self.assertRaisesRegex(RuntimeError, "自动请求 1 次.*筛选条件无效"):
            browser_cdp.search_baiying_candidates(page, settings)

        self.assertEqual(search.click.call_count, 1)
        self.assertNotIn(2_000, [call.args[0] for call in page.wait_for_timeout.call_args_list])

    def test_missing_material_response_gets_at_most_one_retry(self):
        page = FakePage({})
        search = MagicMock()
        page.get_by_text = MagicMock(return_value=MagicMock(first=search))
        page.wait_for_timeout = MagicMock()
        clock = [0.0]

        def tick():
            clock[0] += 2.0
            return clock[0]

        with patch.object(browser_cdp.time, "time", side_effect=tick):
            with self.assertRaisesRegex(RuntimeError, "自动请求 2 次后仍未捕获"):
                browser_cdp.search_baiying_candidates(page, {})

        self.assertEqual(search.click.call_count, 2)
        self.assertIn(2_000, [call.args[0] for call in page.wait_for_timeout.call_args_list])

    def test_page_rate_limit_cooldown_expires_after_one_minute(self):
        page_key = 987654321
        browser_cdp._PAGE_RATE_LIMIT_UNTIL.pop(page_key, None)
        with patch.object(browser_cdp.time, "monotonic", side_effect=[100.0, 100.0, 161.0]), \
                patch.object(browser_cdp.time, "time", side_effect=[1_000.0, 1_000.0, 1_061.0]):
            browser_cdp._mark_page_rate_limited(page_key)
            self.assertEqual(browser_cdp._page_rate_limit_remaining(page_key), 60)
            self.assertEqual(browser_cdp._page_rate_limit_remaining(page_key), 0)
        self.assertNotIn(page_key, browser_cdp._PAGE_RATE_LIMIT_UNTIL)

    def test_profile_rate_limit_cooldown_survives_bridge_restart(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile_path = Path(temporary_directory) / "account-3"
            profile_path.mkdir()
            browser_cdp._configure_rate_limit_profile(profile_path)
            browser_cdp._mark_page_rate_limited(112233)
            state_path = browser_cdp._RATE_LIMIT_STATE_PATH
            self.assertIsNotNone(state_path)
            self.assertTrue(state_path.exists())

            browser_cdp._configure_rate_limit_profile(profile_path)
            self.assertGreater(browser_cdp._page_rate_limit_remaining(445566), 0)

        browser_cdp._RATE_LIMIT_STATE_PATH = None
        browser_cdp._PROFILE_RATE_LIMIT_UNTIL = 0.0
        browser_cdp._PAGE_RATE_LIMIT_UNTIL.clear()

    def test_uncached_query_does_not_hit_platform_during_rate_limit_cooldown(self):
        page = MagicMock()
        page.evaluate.return_value = ""
        settings = {"candidateLimit": 1}
        with patch.object(browser_cdp, "_get_cached_material_list", return_value=None), \
                patch.object(browser_cdp, "_read_search_keyword", return_value=""), \
                patch.object(browser_cdp, "_page_rate_limit_remaining", return_value=42):
            with self.assertRaisesRegex(RuntimeError, "还需约 42 秒.*没有再次请求百应"):
                browser_cdp._apply_one_category_and_collect_candidates(page, settings)

        page.on.assert_not_called()

    def test_failed_11001_followup_sets_page_cooldown(self):
        page = FakePage({})
        page.wait_for_timeout = MagicMock()
        settings = {"candidateLimit": 1, "categorySelection": {
            "level1": ["root"], "level2": ["second"], "level3": ["leaf"]
        }}

        key = id(page)
        browser_cdp._PAGE_RATE_LIMIT_UNTIL.pop(key, None)
        clock = [0.0]

        def tick():
            clock[0] += 2.0
            return clock[0]

        with patch.object(browser_cdp, "apply_filter_settings", return_value=[]), \
                patch.object(browser_cdp, "search_baiying_candidates", side_effect=RuntimeError("material_list 11001")), \
                patch.object(browser_cdp, "_RATE_LIMIT_STATE_PATH", None), \
                patch.object(browser_cdp.time, "time", side_effect=tick):
            with self.assertRaisesRegex(RuntimeError, "60 秒自动冷却"):
                browser_cdp._apply_one_category_and_collect_candidates(page, settings, force_refresh=True)

        self.assertGreater(browser_cdp._page_rate_limit_remaining(key), 0)
        browser_cdp._PAGE_RATE_LIMIT_UNTIL.pop(key, None)
        browser_cdp._PROFILE_RATE_LIMIT_UNTIL = 0.0

    def test_category_apply_11001_stops_without_followup_search(self):
        page = FakePage({})
        page.wait_for_timeout = MagicMock()
        settings = {"categorySelection": {
            "level1": ["root"], "level2": ["second"], "level3": ["leaf"]
        }}

        def emit_network_error(_page, _settings):
            intermediate = FakeResponse(
                {"data": {"summary_promotions": [{
                    "product_id": "must-not-be-consumed",
                    "base_model": {"product_info": {"name": "并行成功列表"}},
                }]}},
                {"filters": [{"key": "MendelCid", "value": {"value": ["leaf"]}}]},
            )
            for callback in list(page.listeners.get("response", [])):
                callback(intermediate)
            response = FakeResponse(
                {"code": 11001, "msg": "当前网络不稳定"},
                {"filters": [{"key": "MendelCid", "value": {"value": ["leaf"]}}]},
            )
            for callback in list(page.listeners.get("response", [])):
                callback(response)
            return []

        with patch.object(browser_cdp, "apply_filter_settings", side_effect=emit_network_error), \
                patch.object(browser_cdp, "search_baiying_candidates") as search, \
                patch.object(browser_cdp, "_mark_page_rate_limited") as mark_rate_limited:
            with self.assertRaisesRegex(RuntimeError, "应用筛选阶段的 material_list 接口返回业务错误 11001.*不再补发"):
                browser_cdp._apply_one_category_and_collect_candidates(page, settings, force_refresh=True)

        search.assert_not_called()
        mark_rate_limited.assert_called_once_with(id(page))
        self.assertNotIn(8_000, [call.args[0] for call in page.wait_for_timeout.call_args_list])

    def test_category_collection_ignores_stale_other_category_response(self):
        fake_page = FakePage({})
        settings = {"candidateLimit": 1, "categorySelection": {
            "level1": ["root"], "level2": ["second"], "level3": ["second__RC_CASCADER_SPLIT__leaf"]
        }}
        stale = {"data": {"summary_promotions": [{
            "product_id": "stale-product", "base_model": {"product_info": {"name": "旧类目商品"}}
        }]}}
        expected = {"ok": True, "candidateCount": 1, "candidates": [
            {"cardIndex": 0, "productId": "fresh-product", "title": "当前类目商品"}
        ], "warnings": []}
        clock = [0.0]

        def tick():
            clock[0] += 2.0
            return clock[0]

        def apply_and_emit(page, _settings):
            response = FakeResponse(stale, {"filters": [{"key": "MendelCid", "value": {"value": ["other"]}}]})
            for callback in page.listeners.get("response", []):
                callback(response)
            return []

        with patch.object(browser_cdp, "apply_filter_settings", side_effect=apply_and_emit), \
                patch.object(browser_cdp, "search_baiying_candidates", return_value=expected) as search, \
                patch.object(browser_cdp.time, "time", side_effect=tick):
            result = browser_cdp._apply_one_category_and_collect_candidates(fake_page, settings, force_refresh=True)

        search.assert_called_once()
        self.assertEqual(result["candidates"], expected["candidates"])
        self.assertTrue(any("忽略 1 个" in warning for warning in result["warnings"]))

    def test_force_refresh_keeps_applied_filters_and_searches_without_replaying_them(self):
        page = MagicMock()
        settings = {
            "candidateLimit": 1,
            "categorySelection": {"level1": ["root"], "level2": ["second"], "level3": ["leaf"]},
        }
        signature = browser_cdp._selection_filter_signature(settings)
        page.evaluate.return_value = signature
        expected = {"ok": True, "candidateCount": 1, "candidates": [
            {"cardIndex": 0, "productId": "fresh-product", "title": "实时结果"}
        ], "warnings": []}

        with patch.object(browser_cdp, "_get_cached_material_list", return_value=None), \
                patch.object(browser_cdp, "_read_search_keyword", return_value=""), \
                patch.object(browser_cdp, "_clear_page_material_cache") as clear_cache, \
                patch.object(browser_cdp, "apply_filter_settings", return_value=[]) as apply, \
                patch.object(browser_cdp, "search_baiying_candidates", return_value=expected) as search:
            result = browser_cdp._apply_one_category_and_collect_candidates(
                page, settings, force_refresh=True
            )

        clear_cache.assert_called_once_with(id(page))
        apply.assert_called_once_with(page, settings)
        search.assert_called_once_with(page, settings, None)
        page.evaluate.assert_called_once_with("() => window.__touguangAppliedFilterSettings || ''")
        page.wait_for_timeout.assert_not_called()
        self.assertEqual(result["candidates"], expected["candidates"])

    def test_stale_search_keyword_invalidates_cached_candidates_and_is_cleared_before_query(self):
        page = FakePage({}, search_keyword="英语口语")
        settings = {
            "candidateLimit": 1,
            "categorySelection": {"level1": ["root"], "level2": ["second"], "level3": ["leaf"]},
        }
        signature = browser_cdp._selection_filter_signature(settings)
        stale_payload = {"data": {"summary_promotions": [{
            "product_id": "stale-keyword-product",
            "base_model": {"product_info": {"name": "旧关键词结果"}},
        }]}}
        key = (id(page), signature)
        browser_cdp._MATERIAL_LIST_CACHE[key] = (browser_cdp.time.monotonic(), stale_payload)
        expected = {"ok": True, "candidateCount": 1, "candidates": [
            {"cardIndex": 0, "productId": "fresh-product", "title": "无旧关键词结果"}
        ], "warnings": []}
        clock = [0.0]

        def tick():
            clock[0] += 2.0
            return clock[0]

        def apply_after_keyword_sync(target_page, _settings):
            self.assertEqual(target_page.search_input.value, "")
            return []

        try:
            with patch.object(browser_cdp, "apply_filter_settings", side_effect=apply_after_keyword_sync), \
                    patch.object(browser_cdp, "search_baiying_candidates", return_value=expected) as search, \
                    patch.object(browser_cdp.time, "time", side_effect=tick):
                result = browser_cdp._apply_one_category_and_collect_candidates(page, settings)

            self.assertEqual(page.search_input.clear_click_count, 1)
            self.assertEqual([row["productId"] for row in result["candidates"]], ["fresh-product"])
            search.assert_called_once_with(page, settings, None)
        finally:
            browser_cdp._MATERIAL_LIST_CACHE.pop(key, None)

    def test_background_filter_dropdown_stabilizer_targets_paused_aurora_entry_transform(self):
        page = MagicMock()

        _stabilize_background_filter_dropdown(page)

        script = page.evaluate.call_args.args[0]
        self.assertIn("touguang-background-filter-dropdown-fix", script)
        self.assertIn("aurora-slide-up-enter-start", script)
        self.assertIn("transform: none !important", script)

    def test_filter_control_uses_same_scoped_list_for_index_and_locator(self):
        page = MagicMock()
        page.evaluate.return_value = 2
        target = MagicMock()
        page.locator.return_value.nth.return_value = target

        result = _filter_control(page, "流量助推")

        self.assertIs(result, target)
        self.assertEqual(
            page.locator.call_args.args[0],
            ".merch-filter-virtual-table .table-row .combine-select-content",
        )

    def test_baiying_cart_status_variants_are_recognized(self):
        candidate = {"inCart": False}
        self.assertTrue(_candidate_is_in_cart(candidate, "商品A 已加选品车"))
        self.assertTrue(_candidate_is_in_cart(candidate, "商品A 已加车"))
        self.assertTrue(_candidate_is_in_cart(candidate, "商品A 去选品车"))
        self.assertTrue(_candidate_is_in_cart({"inCart": True}, "商品A"))
        self.assertFalse(_candidate_is_in_cart(candidate, "商品A 加入选品车"))
        self.assertFalse(_candidate_is_in_cart({"inCart": True}, "商品A 加选品车"))

    def test_cart_response_code_zero_does_not_override_platform_failure_message(self):
        self.assertEqual(
            _cart_response_outcome({"code": 0, "msg": "添加失败：新作者需先完成学习任务"}),
            (False, "添加失败：新作者需先完成学习任务"),
        )
        self.assertEqual(
            _cart_response_outcome({"code": 0, "msg": "添加成功"}),
            (True, "添加成功"),
        )
        self.assertEqual(
            _cart_response_outcome({"code": 0, "data": {"success": False}}),
            (False, "百应加车接口 data.success=false"),
        )
        self.assertFalse(_cart_response_outcome({"msg": "成功但没有业务码"})[0])
        self.assertFalse(_cart_response_outcome({"code": False, "msg": "failed"})[0])

    def test_visible_baiying_cart_error_modal_is_detected(self):
        page = MagicMock()
        page.evaluate.return_value = "添加失败 新作者需先完成学习任务才能电商带货。"
        self.assertEqual(
            _visible_cart_error_message(page),
            "添加失败 新作者需先完成学习任务才能电商带货。",
        )

    def test_baiying_prompt_classifier_distinguishes_captcha_invoice_and_coupon(self):
        self.assertEqual(
            _classify_baiying_prompt("请完成下列验证后继续 按住左边按钮拖动完成上方拼图"),
            "manual_verification",
        )
        self.assertEqual(
            _classify_baiying_prompt("当前商品被商家设定为有开具发票能力的作者才可带货"),
            "invoice_eligibility",
        )
        self.assertEqual(
            _classify_baiying_prompt("本品不可用消费券，带消费券商品享大促流量"),
            "coupon_recommendation",
        )
        self.assertEqual(_classify_baiying_prompt("商品详情"), "")

    def test_visible_baiying_prompt_returns_only_recognized_visible_text(self):
        page = MagicMock()
        page.evaluate.return_value = [
            "普通商品说明",
            "本品不可用消费券，带消费券商品享大促流量",
        ]
        self.assertEqual(
            _visible_baiying_prompt(page),
            ("coupon_recommendation", "本品不可用消费券，带消费券商品享大促流量"),
        )

    def test_invoice_prompt_clicks_only_add_to_cart_action(self):
        page = MagicMock()
        dialogs = page.locator.return_value
        dialogs.count.return_value = 1
        dialog = dialogs.nth.return_value
        dialog.inner_text.return_value = "有开具发票能力的作者才可带货"
        actions = dialog.get_by_role.return_value
        actions.count.return_value = 1
        add_to_cart = actions.first
        add_to_cart.is_visible.return_value = True

        self.assertTrue(_click_invoice_prompt_add_cart(page))

        dialog.get_by_role.assert_called_once_with("button", name="加选品车", exact=True)
        add_to_cart.click.assert_called_once_with(timeout=3_000, no_wait_after=True)

    def test_invoice_prompt_does_not_click_when_add_action_is_ambiguous(self):
        page = MagicMock()
        dialogs = page.locator.return_value
        dialogs.count.return_value = 1
        dialog = dialogs.nth.return_value
        dialog.inner_text.return_value = "有开具发票能力的作者才可带货"
        actions = dialog.get_by_role.return_value
        actions.count.return_value = 2

        self.assertFalse(_click_invoice_prompt_add_cart(page))
        actions.first.click.assert_not_called()

    def test_coupon_recommendation_dismissal_never_interacts_with_recommended_products(self):
        page = MagicMock()
        dialogs = page.locator.return_value
        dialogs.count.return_value = 1
        dialog = dialogs.nth.return_value
        dialog.inner_text.return_value = "本品不可用消费券，带消费券商品享大促流量"
        close_controls = dialog.locator.return_value
        close_controls.count.return_value = 1
        close = close_controls.first
        close.is_visible.return_value = True
        with patch.object(browser_cdp, "_visible_baiying_prompt", side_effect=[
            ("", "")
        ]):
            self.assertTrue(_dismiss_baiying_coupon_recommendation(page))

        close.click.assert_called_once_with(timeout=1_500, no_wait_after=True)
        dialog.get_by_text.assert_not_called()

    def test_captcha_pauses_selected_product_before_any_cart_click(self):
        page = FakePage({})
        candidate = {"cardIndex": 4, "productId": "product-a", "title": "商品A"}
        with patch.object(browser_cdp, "_visible_baiying_prompt", return_value=(
            "manual_verification", "请完成下列验证后继续"
        )), patch.object(browser_cdp, "_locate_candidate_card") as locate:
            result = execute_selected_candidates(page, MagicMock(), [candidate], [4], ".")

        self.assertFalse(result[0]["cartAdded"])
        self.assertEqual(result[0]["cartStatus"], "等待手动验证")
        self.assertEqual(result[0]["manualAction"], "complete_baiying_verification")
        self.assertEqual(result[0]["productId"], "product-a")
        self.assertFalse(result[0]["cartAttempted"])
        self.assertIn("恢复时会按原商品 ID 复核", result[0]["status"])
        locate.assert_not_called()
        self.assertEqual(page.listeners, {})

    def test_invoice_prompt_clicks_add_cart_once_and_keeps_eligibility_warning(self):
        page = FakePage({})
        title = MagicMock()
        card = MagicMock()
        card.inner_text.side_effect = ["", "商品A 已加选品车", "商品A 已加选品车"]
        add_button = MagicMock()
        add_button.count.return_value = 1
        card.locator.return_value.first = add_button
        candidate = {"cardIndex": 0, "productId": "product-a", "title": "商品A"}
        invoice_text = "有开具发票能力的作者才可带货"
        with patch.object(browser_cdp, "_visible_baiying_prompt", side_effect=[
            ("", ""), ("", ""), ("invoice_eligibility", invoice_text), ("", "")
        ]), patch.object(browser_cdp, "_locate_candidate_card", return_value=(title, card)), \
                patch.object(browser_cdp, "_click_invoice_prompt_add_cart") as click_invoice:
            def confirm_add_to_cart(_page):
                response = MagicMock()
                response.url = "https://buyin.jinritemai.com/selection_cart_pc_api/card/m_add_card"
                response.json.return_value = {"code": 0, "msg": "加车成功"}
                for callback in list(page.listeners.get("response", [])):
                    callback(response)
                return True

            click_invoice.side_effect = confirm_add_to_cart
            result = execute_selected_candidates(
                page, MagicMock(), [candidate], [0], ".", stop_after_cart=True,
            )

        self.assertTrue(result[0]["cartAdded"], result[0])
        self.assertEqual(result[0]["cartStatus"], "百应接口已确认加车")
        self.assertEqual(click_invoice.call_count, 1)
        self.assertIn("开具佣金发票资质", result[0]["platformWarnings"][0])
        self.assertIn("平台提醒", result[0]["status"])
        add_button.click.assert_called_once_with(timeout=8_000, no_wait_after=True)

    def test_captcha_on_product_detail_pauses_after_cart_without_retrying_cart(self):
        page = FakePage({})
        context = MagicMock()
        title = MagicMock()
        card = MagicMock()
        card.inner_text.side_effect = ["", "商品A 已加选品车", "商品A 已加选品车"]
        add_button = MagicMock()
        add_button.count.return_value = 1
        card.locator.return_value.first = add_button
        candidate = {"cardIndex": 0, "productId": "product-a", "title": "商品A"}
        with patch.object(browser_cdp, "_visible_baiying_prompt", side_effect=[
            ("", ""), ("", ""), ("", ""),
            ("manual_verification", "请完成下列验证后继续"),
        ]), patch.object(browser_cdp, "_locate_candidate_card", return_value=(title, card)):
            def add_response(**_kwargs):
                response = MagicMock()
                response.url = "https://buyin.jinritemai.com/selection_cart_pc_api/card/m_add_card"
                response.json.return_value = {"code": 0, "msg": "加车成功"}
                for callback in list(page.listeners.get("response", [])):
                    callback(response)

            add_button.click.side_effect = add_response
            result = execute_selected_candidates(
                page, context, [candidate], [0], ".", stop_after_cart=False,
            )

        self.assertTrue(result[0]["cartAdded"])
        self.assertEqual(result[0]["manualAction"], "complete_baiying_verification")
        self.assertIn("不会重复加车", result[0]["status"])
        self.assertEqual(result[0]["productId"], "product-a")
        add_button.click.assert_called_once()
        context.request.get.assert_not_called()

    def test_coupon_prompt_closes_then_rechecks_the_same_product_id(self):
        page = FakePage({})
        title = MagicMock()
        card = MagicMock()
        card.inner_text.side_effect = ["", "商品A 已加选品车", "商品A 已加选品车"]
        add_button = MagicMock()
        add_button.count.return_value = 1
        card.locator.return_value.first = add_button
        candidate = {"cardIndex": 2, "productId": "product-a", "title": "商品A"}
        coupon_text = "本品不可用消费券，带消费券商品享大促流量"
        with patch.object(browser_cdp, "_visible_baiying_prompt", side_effect=[
            ("", ""), ("", ""), ("coupon_recommendation", coupon_text), ("", "")
        ]), patch.object(browser_cdp, "_locate_candidate_card", return_value=(title, card)) as locate, \
                patch.object(browser_cdp, "_dismiss_baiying_coupon_recommendation", return_value=True):
            def add_response(**_kwargs):
                response = MagicMock()
                response.url = "https://buyin.jinritemai.com/selection_cart_pc_api/card/m_add_card"
                response.json.return_value = {"code": 0, "msg": "加车成功"}
                for callback in list(page.listeners.get("response", [])):
                    callback(response)

            add_button.click.side_effect = add_response
            result = execute_selected_candidates(
                page, MagicMock(), [candidate], [2], ".", stop_after_cart=True,
            )

        self.assertTrue(result[0]["cartAdded"], result[0])
        self.assertEqual(result[0]["productId"], "product-a")
        self.assertTrue(any("未点击推荐商品" in text for text in result[0]["platformNotices"]))
        self.assertEqual(locate.call_count, 2)
        add_button.click.assert_called_once()

    def test_previous_uncertain_cart_attempt_requires_explicit_add_button_before_retry(self):
        page = FakePage({})
        title = MagicMock()
        card = MagicMock()
        card.inner_text.return_value = "商品A 商品详情"
        button = MagicMock()
        button.count.return_value = 1
        card.locator.return_value.first = button
        candidate = {
            "cardIndex": 0, "productId": "product-a", "title": "商品A", "cartAttempted": True
        }
        with patch.object(browser_cdp, "_visible_baiying_prompt", return_value=("", "")), \
                patch.object(browser_cdp, "_locate_candidate_card", return_value=(title, card)):
            result = execute_selected_candidates(page, MagicMock(), [candidate], [0], ".")

        self.assertFalse(result[0]["cartAdded"])
        self.assertEqual(result[0]["cartStatus"], "状态待人工核验")
        self.assertTrue(result[0]["cartAttempted"])
        self.assertIn("避免重复操作", result[0]["status"])
        button.click.assert_not_called()

    def test_candidate_add_cart_action_text_is_exact_negative_cart_evidence(self):
        self.assertTrue(_candidate_has_add_cart_action_text("商品A 加选品车"))
        self.assertTrue(_candidate_has_add_cart_action_text("商品A 加入选品车"))
        self.assertFalse(_candidate_has_add_cart_action_text("商品A 已加选品车"))

    def test_detail_recovery_skips_cart_button_for_baiying_cart_copy(self):
        class FakeContext:
            def __init__(self):
                self.listeners = {}
                self.request = MagicMock()
                response = MagicMock()
                response.ok = True
                response.headers = {"content-type": "image/jpeg"}
                response.body.return_value = b"jpeg-bytes"
                self.request.get.return_value = response

            def on(self, event, callback):
                self.listeners.setdefault(event, []).append(callback)

            def remove_listener(self, event, callback):
                self.listeners[event].remove(callback)

        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage({})
            context = FakeContext()
            title = MagicMock()
            card = MagicMock()
            card.inner_text.return_value = "商品A 已加选品车"
            button = MagicMock()
            card.locator.return_value.first = button

            def open_detail(**_kwargs):
                response = MagicMock()
                response.url = "https://buyin.jinritemai.com/pack_detail"
                response.json.return_value = {}
                for callback in list(context.listeners.get("response", [])):
                    callback(response)

            title.click.side_effect = open_detail
            candidate = {
                "cardIndex": 0, "productId": "product-a", "title": "商品A", "inCart": False,
                "sourceCategorySelection": {"level1": ["food"], "level2": ["drinks"], "level3": ["tea"]},
            }
            with patch.object(browser_cdp, "_candidate_is_current_for_source", return_value=True), \
                    patch.object(browser_cdp, "_locate_candidate_card", return_value=(title, card)), \
                    patch.object(browser_cdp, "_product_gallery_urls", return_value=(
                        "product-a", "商品A", ["https://example.test/product.jpg"],
                        "https://haohuo.jinritemai.com/views/product/item2?id=product-a",
                    )):
                result = execute_selected_candidates(
                    page, context, [candidate], [0], temp_dir,
                    {"categorySelection": candidate["sourceCategorySelection"]},
                )

            self.assertTrue(result[0]["cartAdded"])
            self.assertEqual(result[0]["cartStatus"], "已在选品车")
            self.assertEqual(result[0]["productUrl"], "https://haohuo.jinritemai.com/views/product/item2?id=product-a")
            self.assertEqual(len(result[0]["imagePaths"]), 1)
            button.click.assert_not_called()

    def test_filter_option_uses_forced_browser_click(self):
        option = MagicMock()

        _click_filter_option(option)

        option.click.assert_called_once_with(force=True, timeout=3_000, no_wait_after=True)

    def test_legacy_short_third_category_id_gets_current_parent_path(self):
        self.assertEqual(
            _category_path_id("1000004649", "1000004647"),
            "1000004647__RC_CASCADER_SPLIT__1000004649",
        )
        self.assertEqual(
            _category_path_id("1000004647__RC_CASCADER_SPLIT__1000004649", "1000004647"),
            "1000004647__RC_CASCADER_SPLIT__1000004649",
        )

    def test_category_selection_uses_forced_browser_click(self):
        item = MagicMock()
        content = MagicMock()
        item.locator.return_value = content

        _click_category_item(item)

        item.locator.assert_called_once_with(".aurora-cascader-menu-item-content")
        content.click.assert_called_once_with(force=True, timeout=3_000, no_wait_after=True)
        content.evaluate.assert_not_called()

    def test_category_expand_uses_hover_for_react_handler(self):
        expand = MagicMock()

        _hover_category_expand(expand)

        expand.hover.assert_called_once_with(force=True, timeout=3_000)
        expand.evaluate.assert_not_called()

    def test_background_category_waits_use_interval_polling(self):
        page = MagicMock()

        _wait_category_item_present(page, "second__RC_CASCADER_SPLIT__leaf", timeout_ms=8_000)
        self.assertEqual(page.wait_for_function.call_args.kwargs["polling"], 250)

        page.wait_for_function.reset_mock()
        _wait_category_item_checked(page, "second__RC_CASCADER_SPLIT__leaf", timeout_ms=4_000)
        self.assertEqual(page.wait_for_function.call_args.kwargs["polling"], 250)

    def test_background_page_uses_cdp_background_target(self):
        class ExpectedPage:
            def __init__(self, page):
                self.value = page

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeContext:
            def __init__(self, page):
                self.page = page

            def expect_page(self, timeout):
                self.timeout = timeout
                return ExpectedPage(self.page)

        class FakeCdp:
            def __init__(self):
                self.calls = []
                self.detached = False

            def send(self, method, params):
                self.calls.append((method, params))
                return {"targetId": "background-target"}

            def detach(self):
                self.detached = True

        page = object()
        context = FakeContext(page)
        cdp = FakeCdp()
        browser = MagicMock()
        browser.new_browser_cdp_session.return_value = cdp

        self.assertIs(_create_background_page(context, browser), page)
        self.assertEqual(context.timeout, 10_000)
        self.assertEqual(cdp.calls, [(
            "Target.createTarget",
            {"url": "about:blank", "background": True, "focus": False},
        )])
        self.assertTrue(cdp.detached)

    def test_visible_managed_page_is_left_open_and_replaced_by_background_page(self):
        visible_page = MagicMock()
        visible_page.url = "https://buyin.jinritemai.com/dashboard/merch-picking-library"
        visible_page.is_closed.return_value = False

        def page_evaluate(script):
            if "window.__touguangManagedSelectionPage === true" in script:
                return True
            if "document.visibilityState === 'hidden'" in script:
                return False
            return None

        visible_page.evaluate.side_effect = page_evaluate
        background_page = MagicMock()
        background_page.url = "about:blank"
        context = MagicMock()
        context.pages = [visible_page]
        browser = MagicMock()

        with patch.object(browser_cdp, "_create_background_page", return_value=background_page) as create_page, \
                patch.object(browser_cdp, "attach_page"), \
                patch.object(browser_cdp, "wait_for_selection_page_ready", return_value=True):
            result = find_selection_page(context, attached=set(), browser=browser)

        self.assertIs(result, background_page)
        create_page.assert_called_once_with(context, browser)
        visible_page.close.assert_not_called()
        background_page.goto.assert_called_once()

    def test_category_parent_expands_to_actionable_leaf_categories(self):
        food_root = {"name": "食品饮料", "children": [
            {"id": "snacks", "name": "休闲食品", "children": [
                {"id": "snack-a", "name": "膨化食品"},
                {"id": "snack-b", "name": "坚果炒货"},
            ]},
            {"id": "drinks", "name": "水饮冲调", "children": [
                {"id": "drink-a", "name": "乳制品"},
                {"id": "drink-b", "name": "茶叶"},
            ]},
        ]}

        targets = _category_targets_for_root(food_root, set(), set())
        self.assertEqual(
            [(level, node["id"], parent) for level, node, parent in targets],
            [
                (3, "snack-a", "snacks"),
                (3, "snack-b", "snacks"),
                (3, "drink-a", "drinks"),
                (3, "drink-b", "drinks"),
            ],
        )

    def test_selected_second_category_expands_only_its_descendants(self):
        food_root = {"name": "食品饮料", "children": [
            {"id": "snacks", "name": "休闲食品", "children": [
                {"id": "snack-a", "name": "膨化食品"},
            ]},
            {"id": "drinks", "name": "水饮冲调", "children": [
                {"id": "drink-a", "name": "乳制品"},
                {"id": "drink-b", "name": "茶叶"},
            ]},
        ]}
        self.assertEqual(
            [(level, node["id"], parent) for level, node, parent in
             _category_targets_for_root(food_root, {"drinks"}, set())],
            [(3, "drink-a", "drinks"), (3, "drink-b", "drinks")],
        )

    def test_selected_third_category_does_not_expand_its_siblings(self):
        food_root = {"name": "食品饮料", "children": [
            {"id": "drinks", "name": "水饮冲调", "children": [
                {"id": "drink-a", "name": "乳制品"},
                {"id": "drink-b", "name": "茶叶"},
            ]},
        ]}
        targets = _category_targets_for_root(food_root, {"drinks"}, {"drink-b"})
        self.assertEqual([(level, node["id"], parent) for level, node, parent in targets],
                         [(3, "drink-b", "drinks")])

    def test_second_category_without_children_is_selected_directly(self):
        root = {"name": "服饰", "children": [
            {"id": "shoes", "name": "鞋类", "children": []},
        ]}
        targets = _category_targets_for_root(root, set(), set())
        self.assertEqual([(level, node["id"], parent) for level, node, parent in targets],
                         [(2, "shoes", None)])

    def test_multiselect_category_settings_split_into_one_leaf_query_each(self):
        settings = {
            "candidateLimit": 3,
            "categorySelection": {
                "level1": ["food", "care"],
                "level2": ["drinks", "cleaning"],
                "level3": ["tea", "soap"],
            },
            "categoryTree": [
                {"id": "food", "children": [{"id": "drinks", "children": [
                    {"id": "tea", "children": []}, {"id": "milk", "children": []}
                ]}]},
                {"id": "care", "children": [{"id": "cleaning", "children": [
                    {"id": "soap", "children": []}, {"id": "towel", "children": []}
                ]}]},
            ],
        }

        queries = _category_query_settings(settings)

        self.assertEqual([query["categorySelection"] for query in queries], [
            {"level1": ["food"], "level2": ["drinks"], "level3": ["tea"]},
            {"level1": ["care"], "level2": ["cleaning"], "level3": ["soap"]},
        ])

    def test_mismatched_selected_roots_are_blocked_before_29_query_fanout(self):
        food_seconds = [
            {"id": "snacks", "name": "休闲食品", "children": [
                {"id": f"food-{index}", "name": f"食品三级类目 {index}", "children": []}
                for index in range(1, 4)
            ]},
            {"id": "drinks", "name": "水饮冲调", "children": [
                {"id": f"drink-{index}", "name": f"饮料三级类目 {index}", "children": []}
                for index in range(1, 4)
            ]},
            {"id": "grains", "name": "粮油干货", "children": [
                {"id": f"grain-{index}", "name": f"粮油三级类目 {index}", "children": []}
                for index in range(1, 3)
            ]},
        ]
        beauty = {"id": "beauty", "name": "美妆", "children": [
            {"id": "makeup", "name": "彩妆香水", "children": [
                {"id": f"beauty-{index}", "name": f"美妆三级类目 {index}", "children": []}
                for index in range(1, 9)
            ]},
        ]}
        shoes = {"id": "shoes", "name": "鞋靴箱包", "children": [
            {"id": "bags", "name": "箱包", "children": [
                {"id": f"bag-{index}", "name": f"箱包三级类目 {index}", "children": []}
                for index in range(1, 7)
            ]},
            {"id": "footwear", "name": "鞋靴", "children": [
                {"id": f"shoe-{index}", "name": f"鞋靴三级类目 {index}", "children": []}
                for index in range(1, 8)
            ]},
        ]}
        settings = {
            "categorySelection": {
                "level1": ["food", "beauty", "shoes"],
                "level2": ["snacks", "drinks", "grains"],
                "level3": [f"food-{index}" for index in range(1, 4)] +
                          [f"drink-{index}" for index in range(1, 4)] +
                          [f"grain-{index}" for index in range(1, 3)],
            },
            "categoryTree": [
                {"id": "food", "name": "食品饮料", "children": food_seconds}, beauty, shoes,
            ],
        }

        with self.assertRaisesRegex(RuntimeError, "29 条查询") as caught:
            _category_query_settings(settings)

        message = str(caught.exception)
        self.assertIn("美妆（beauty）", message)
        self.assertIn("鞋靴箱包（shoes）", message)
        self.assertIn("美妆三级类目 8", message)
        self.assertIn("鞋靴三级类目 7", message)
        self.assertIn("类目层级配置冲突", message)

    def test_consistent_eight_leaf_selection_plans_exactly_eight_queries(self):
        seconds = [
            {"id": "snacks", "children": [
                {"id": "ice-cream", "children": []}, {"id": "nuts", "children": []},
                {"id": "seafood-snacks", "children": []},
            ]},
            {"id": "drinks", "children": [
                {"id": "dairy", "children": []}, {"id": "coffee", "children": []},
                {"id": "tea", "children": []},
            ]},
            {"id": "grains", "children": [
                {"id": "canned", "children": []}, {"id": "additives", "children": []},
            ]},
        ]
        settings = {
            "categorySelection": {
                "level1": ["food"],
                "level2": ["snacks", "drinks", "grains"],
                "level3": ["ice-cream", "nuts", "seafood-snacks", "dairy", "coffee", "tea", "canned", "additives"],
            },
            "categoryTree": [{"id": "food", "name": "食品饮料", "children": seconds}],
        }

        queries = _category_query_settings(settings)

        self.assertEqual(len(queries), 8)
        self.assertEqual({query["categorySelection"]["level1"][0] for query in queries}, {"food"})

    def test_explicit_root_only_selection_keeps_parent_expansion_semantics(self):
        settings = {
            "categorySelection": {"level1": ["food"], "level2": [], "level3": []},
            "categoryTree": [{"id": "food", "children": [
                {"id": "snacks", "children": [
                    {"id": "nuts", "children": []}, {"id": "candy", "children": []},
                ]},
            ]}],
        }

        queries = _category_query_settings(settings)

        self.assertEqual(len(queries), 2)
        self.assertEqual(
            [query["categorySelection"]["level3"][0] for query in queries], ["nuts", "candy"]
        )

    def test_inconsistent_category_settings_stop_before_candidate_collection(self):
        settings = {
            "categorySelection": {"level1": ["food", "extra"], "level2": ["snacks"], "level3": ["leaf"]},
            "categoryTree": [
                {"id": "food", "name": "食品", "children": [{"id": "snacks", "children": [
                    {"id": "leaf", "children": []},
                ]}]},
                {"id": "extra", "name": "其他", "children": [{"id": "all", "children": [
                    {"id": "other-a", "children": []}, {"id": "other-b", "children": []},
                ]}]},
            ],
        }
        page = MagicMock()

        with patch.object(browser_cdp, "_apply_one_category_and_collect_candidates") as apply:
            with self.assertRaisesRegex(RuntimeError, "类目层级配置冲突"):
                apply_settings_and_collect_candidates(page, settings)

        apply.assert_not_called()
        self.assertEqual(page.method_calls, [])

        command = {
            "settings": settings,
            "candidateSnapshot": [{"cardIndex": 1, "productId": "p-1", "title": "候选商品"}],
            "selectedIndexes": [1],
        }
        with self.assertRaisesRegex(RuntimeError, "类目层级配置冲突"):
            run_selection_command(page, MagicMock(), command)
        self.assertEqual(page.method_calls, [])

    def test_captcha_at_command_start_stops_before_any_candidate_request(self):
        page = MagicMock()
        command = {"settings": {}, "previewOnly": False}
        with patch.object(browser_cdp, "_visible_baiying_prompt", return_value=(
            "manual_verification", "请完成下列验证后继续"
        )), patch.object(browser_cdp, "apply_settings_and_collect_candidates") as collect:
            with self.assertRaisesRegex(RuntimeError, "百应要求手动完成滑块验证"):
                run_selection_command(page, MagicMock(), command)

        collect.assert_not_called()
        self.assertEqual(page.method_calls, [])

    def test_captcha_during_material_list_wait_stops_the_current_fetch_immediately(self):
        page = MagicMock()
        collector = browser_cdp._MaterialListCollector({})
        with patch.object(browser_cdp, "_visible_baiying_prompt", return_value=(
            "manual_verification", "请完成下列验证后继续"
        )):
            with self.assertRaisesRegex(RuntimeError, "百应要求手动完成滑块验证"):
                browser_cdp._wait_for_material_list_settled(
                    page, collector, 0, browser_cdp.time.time() + 10, quiet_seconds=1
                )

        page.wait_for_timeout.assert_not_called()

    def test_uncertain_cart_attempt_marker_survives_candidate_refresh_by_product_id(self):
        page = MagicMock()
        original = {"cardIndex": 1, "productId": "product-a", "title": "商品A", "cartAttempted": True}
        refreshed = {"cardIndex": 7, "productId": "product-a", "title": "商品A"}
        command = {
            "settings": {},
            "candidateSnapshot": [original],
            "selectedIndexes": [1],
            "refreshCandidatesById": True,
        }
        with patch.object(browser_cdp, "_visible_baiying_prompt", return_value=("", "")), \
                patch.object(browser_cdp, "_candidate_card_is_available", return_value=False), \
                patch.object(browser_cdp, "apply_settings_and_collect_candidates", return_value={
                    "candidates": [refreshed], "warnings": []
                }), patch.object(browser_cdp, "_exclude_unrendered_selected_candidates", return_value=([refreshed], [])), \
                patch.object(browser_cdp, "execute_selected_candidates", return_value=[{
                    "productId": "product-a", "cartAdded": False
                }]) as execute:
            result = run_selection_command(page, MagicMock(), command)

        self.assertEqual(result["completionCount"], 1)
        self.assertEqual(result["completed"][0]["productId"], "product-a")
        execution_candidates = execute.call_args.args[2]
        self.assertEqual(execution_candidates[0]["cardIndex"], 7)
        self.assertTrue(execution_candidates[0]["cartAttempted"])

    def test_multi_category_candidate_results_are_merged_deduped_and_ranked(self):
        settings = {
            "candidateLimit": 2,
            "trendPreference": "上升优先",
            "categorySelection": {"level1": ["food"], "level2": ["drinks"], "level3": ["tea", "milk"]},
            "categoryTree": [{"id": "food", "children": [{"id": "drinks", "children": [
                {"id": "tea", "children": []}, {"id": "milk", "children": []}
            ]}]}],
        }
        results = [
            {"ok": True, "candidateCount": 2, "warnings": [], "candidates": [
                {"cardIndex": 4, "productId": "duplicate", "title": "重复商品", "poolRank": 2,
                 "trendRising": False, "saleAmount": 100},
                {"cardIndex": 5, "productId": "first-only", "title": "第一个类目商品", "poolRank": 1,
                 "trendRising": False, "saleAmount": 20},
            ]},
            {"ok": True, "candidateCount": 2, "warnings": ["第二类目提示"], "candidates": [
                {"cardIndex": 0, "productId": "duplicate", "title": "重复商品", "poolRank": 0,
                 "trendRising": True, "saleAmount": 300},
                {"cardIndex": 1, "productId": "second-only", "title": "第二个类目商品", "poolRank": 1,
                 "trendRising": True, "saleAmount": 30},
            ]},
        ]
        with patch.object(
            browser_cdp, "_page_applied_filter_signature",
            return_value=browser_cdp._selection_filter_signature(settings),
        ), patch.object(browser_cdp, "_apply_one_category_and_collect_candidates", side_effect=results) as apply:
            merged = apply_settings_and_collect_candidates(MagicMock(), settings)

        self.assertEqual(apply.call_count, 2)
        self.assertFalse(apply.call_args_list[0].kwargs["preserve_other_filters"])
        self.assertTrue(apply.call_args_list[1].kwargs["preserve_other_filters"])
        self.assertTrue(apply.call_args_list[0].kwargs["replace_category_selection"])
        self.assertTrue(apply.call_args_list[1].kwargs["replace_category_selection"])
        self.assertEqual([item["productId"] for item in merged["candidates"]], ["duplicate", "second-only"])
        self.assertEqual(merged["candidates"][0]["cardIndex"], 0)
        self.assertEqual(merged["candidates"][0]["sourceCategorySelection"], {
            "level1": ["food"], "level2": ["drinks"], "level3": ["milk"]
        })
        self.assertEqual(merged["warnings"], ["第二类目提示"])

    def test_category_replacement_clears_only_checked_items_outside_current_query(self):
        root = {"id": "root", "children": [{"id": "second", "children": [
            {"id": "leaf-a", "children": []}, {"id": "leaf-b", "children": []}
        ]}]}
        other_root = {"id": "other-root", "children": [{"id": "other-second", "children": [
            {"id": "other-leaf", "children": []}
        ]}]}
        page = MagicMock()
        page.evaluate.return_value = [
            "second__RC_CASCADER_SPLIT__leaf-a",
            "second__RC_CASCADER_SPLIT__leaf-b",
            "other-second__RC_CASCADER_SPLIT__other-leaf",
        ]
        with patch.object(browser_cdp, "_clear_platform_category_root") as clear_root:
            browser_cdp._clear_extra_platform_categories(
                page,
                {"root": root},
                {"second"},
                {"second__RC_CASCADER_SPLIT__leaf-b"},
                [root, other_root],
            )

        self.assertEqual(clear_root.call_count, 2)
        clear_root.assert_any_call(page, "root", {
            "second", "second__RC_CASCADER_SPLIT__leaf-a", "second__RC_CASCADER_SPLIT__leaf-b"
        })
        clear_root.assert_any_call(page, "other-root", {
            "other-second", "other-second__RC_CASCADER_SPLIT__other-leaf"
        })

    def test_multi_category_failure_reports_query_index_and_cached_progress(self):
        settings = {
            "candidateLimit": 1,
            "categorySelection": {
                "level1": ["root"], "level2": ["second"],
                "level3": ["leaf-a", "leaf-b"],
            },
            "categoryTree": [{"id": "root", "children": [{"id": "second", "children": [
                {"id": "leaf-a", "children": []}, {"id": "leaf-b", "children": []}
            ]}]}],
        }
        page = MagicMock()
        with patch.object(browser_cdp, "_apply_one_category_and_collect_candidates", side_effect=[
            {"candidates": [], "warnings": []},
            RuntimeError("平台限流 11001"),
        ]) as apply_one, patch.object(
            browser_cdp, "_can_reuse_applied_non_category_filters", return_value=True
        ):
            with self.assertRaisesRegex(
                RuntimeError, "第 2/2 项.*leaf-b.*前 1 项成功结果仅在当前桥接进程内缓存 15 分钟.*平台限流"
            ):
                browser_cdp.apply_settings_and_collect_candidates(page, settings)

        self.assertEqual(apply_one.call_count, 2)

    def test_category_replacement_stops_on_unknown_active_category(self):
        root = {"id": "root", "children": [{"id": "second", "children": [
            {"id": "leaf", "children": []}
        ]}]}
        page = MagicMock()
        page.evaluate.return_value = ["unknown-category-id"]
        with self.assertRaisesRegex(RuntimeError, "无法识别的已选类目"):
            browser_cdp._clear_extra_platform_categories(
                page, {"root": root}, {"second"}, {"leaf"}, [root]
            )

    def test_cart_recovery_requeries_candidate_source_category_by_product_id(self):
        candidate = {
            "cardIndex": 3, "productId": "product-a", "title": "商品A",
            "sourceCategorySelection": {"level1": ["food"], "level2": ["drinks"], "level3": ["tea"]},
        }
        settings = {"categorySelection": {"level1": ["food"], "level2": ["drinks"], "level3": ["tea"]}}
        with patch.object(browser_cdp, "_apply_one_category_and_collect_candidates", return_value={
            "candidates": [], "warnings": []
        }) as refresh:
            result = execute_selected_candidates(MagicMock(), MagicMock(), [candidate], [3], ".", settings)

        self.assertEqual(result[0]["cartAdded"], False)
        self.assertIn("未点击加车", result[0]["status"])
        self.assertEqual(refresh.call_args.args[1]["categorySelection"], candidate["sourceCategorySelection"])
        self.assertTrue(refresh.call_args.kwargs["force_refresh"])
        self.assertEqual(refresh.call_args.kwargs["preserve_product_ids"], ["product-a"])

    def test_current_source_candidate_skips_duplicate_list_refresh_before_cart(self):
        candidate = {
            "cardIndex": 0, "productId": "product-a", "title": "商品A",
            "sourceCategorySelection": {"level1": ["food"], "level2": ["drinks"], "level3": ["tea"]},
        }
        page = FakePage({})
        title = MagicMock()
        card = MagicMock()
        card.inner_text.return_value = ""
        button = MagicMock()
        button.count.return_value = 1
        card.locator.return_value.first = button

        def click_add_button(**_kwargs):
            response = MagicMock()
            response.url = "https://buyin.jinritemai.com/selection_cart_pc_api/card/m_add_card"
            response.json.return_value = {"code": 0, "msg": "加车成功"}
            for callback in list(page.listeners.get("response", [])):
                callback(response)

        button.click.side_effect = click_add_button
        with patch.object(browser_cdp, "_candidate_is_current_for_source", return_value=True) as is_current, \
                patch.object(browser_cdp, "_apply_one_category_and_collect_candidates") as refresh, \
                patch.object(browser_cdp, "_locate_candidate_card", return_value=(title, card)):
            result = execute_selected_candidates(
                page, MagicMock(), [candidate], [0], ".",
                {"categorySelection": candidate["sourceCategorySelection"]}, stop_after_cart=True,
            )

        is_current.assert_called_once()
        refresh.assert_not_called()
        self.assertTrue(result[0]["cartAdded"])
        button.click.assert_called_once()

    def test_stop_after_cart_does_not_open_details_or_download_images(self):
        page = FakePage({})
        context = MagicMock()
        title = MagicMock()
        card = MagicMock()
        card.inner_text.return_value = ""
        button = MagicMock()
        button.count.return_value = 1
        card.locator.return_value.first = button

        def click_add_button(**_kwargs):
            response = MagicMock()
            response.url = "https://buyin.jinritemai.com/selection_cart_pc_api/card/m_add_card"
            response.json.return_value = {"code": 0, "msg": "加车成功"}
            for callback in list(page.listeners.get("response", [])):
                callback(response)

        button.click.side_effect = click_add_button
        with patch.object(browser_cdp, "_locate_candidate_card", return_value=(title, card)):
            result = execute_selected_candidates(
                page, context,
                [{"cardIndex": 0, "productId": "product-a", "title": "商品A"}],
                [0], ".", stop_after_cart=True,
            )

        self.assertEqual(result[0]["cartAdded"], True)
        self.assertEqual(result[0]["cartStatus"], "百应接口已确认加车")
        self.assertIn("停止于加车阶段", result[0]["status"])
        title.click.assert_not_called()
        context.on.assert_not_called()

    def test_filter_application_uses_live_category_dependent_options_when_cache_is_stale(self):
        page = MagicMock()
        page.evaluate.return_value = ""
        page.get_by_text.return_value.last.count.return_value = 0
        control = MagicMock()
        menu = MagicMock()
        menu.get_attribute.return_value = "aurora-select-dropdown-multiple"
        menu.get_by_text.return_value.count.return_value = 0
        options = menu.locator.return_value
        options.all_inner_texts.return_value = ["商家投千川", "自动投千川"]
        option = MagicMock()
        option.get_attribute.return_value = ""
        options.nth.return_value = option
        settings = {
            "filters": {"delivery": {}, "product": {"流量助推": ["自动投千川", "商家投千川"]}, "features": []},
            "filterCatalog": {"filters": {"流量助推": ["不限", "≥80%"]}, "features": []},
        }

        with patch.object(browser_cdp, "apply_category_settings", return_value=[]), \
                patch.object(browser_cdp, "_open_filter_dropdown", return_value=(control, menu)), \
                patch.object(browser_cdp, "_close_filter_dropdown"), \
                patch.object(browser_cdp, "_click_filter_option") as click_option:
            warnings = browser_cdp.apply_filter_settings(page, settings)

        self.assertEqual(click_option.call_count, 2)
        self.assertTrue(any("按当前页面选项执行" in warning for warning in warnings))

    def test_filter_reset_waits_before_applying_categories(self):
        page = MagicMock()
        reset = MagicMock()
        reset.count.return_value = 1
        reset.is_visible.return_value = True
        page.get_by_text.return_value.last = reset
        events = []
        page.wait_for_timeout.side_effect = lambda value: events.append(("wait", value))
        settings = {"filters": {"delivery": {}, "product": {}, "features": []}}

        def apply_categories(*_args):
            events.append(("categories", None))
            return []

        with patch.object(browser_cdp, "apply_category_settings", side_effect=apply_categories):
            browser_cdp.apply_filter_settings(page, settings)

        self.assertEqual(events[0], ("wait", 1_000))
        self.assertEqual(events[1], ("categories", None))

    def test_filter_dropdown_reopens_once_when_options_load_late(self):
        page = MagicMock()
        page.wait_for_function.side_effect = [browser_cdp.PlaywrightTimeoutError("延迟加载"), None]
        control = MagicMock()
        menu = MagicMock()
        menu.get_attribute.return_value = "aurora-select-dropdown-multiple"
        menu.is_visible.return_value = True
        menu.locator.return_value.count.return_value = 2
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.nth.return_value = menu

        with patch.object(browser_cdp, "_filter_control", return_value=control), \
                patch.object(browser_cdp, "_wait_filter_dropdown_closed"), \
                patch.object(browser_cdp, "_close_filter_dropdown"):
            result = browser_cdp._open_filter_dropdown(page, "流量助推")

        self.assertIs(result[0], control)
        self.assertIs(result[1], menu)
        self.assertEqual(page.wait_for_function.call_count, 2)
        self.assertEqual(control.click.call_count, 2)

    def test_category_dropdown_is_selected_by_target_id_not_last_portal(self):
        target = FakeCategoryDropdown(["1000002395"])
        unrelated = FakeCategoryDropdown(["1000000724"])
        page = MagicMock()
        page.locator.return_value = FakeCategoryDropdowns([target, unrelated])

        self.assertIs(_category_dropdown_for_item(page, "1000002395"), target)

    def test_category_control_prefers_arrow_over_tooltip_covered_text(self):
        class Arrow:
            def __init__(self):
                self.clicked = False

            def count(self):
                return 1

            def is_visible(self):
                return True

            def click(self, **kwargs):
                self.clicked = kwargs

        class Parent:
            def __init__(self, arrow):
                self.arrow = arrow

            def locator(self, selector):
                self.selector = selector
                return self.arrow

        class Control:
            def __init__(self, parent):
                self.parent = parent
                self.clicked = False

            def locator(self, selector):
                self.selector = selector
                return self.parent

            def click(self, **kwargs):
                self.clicked = kwargs

        arrow = Arrow()
        parent = Parent(arrow)
        control = Control(parent)
        page = MagicMock()
        page.evaluate.return_value = {"x": 17, "y": 23}
        browser_cdp._click_category_control(page, control)

        self.assertEqual(control.selector, "xpath=..")
        self.assertEqual(parent.selector, '[aria-label="down"], [aria-label="up"]')
        self.assertEqual(arrow.clicked, {
            "force": True, "timeout": 3_000, "no_wait_after": True
        })
        self.assertFalse(control.clicked)
        page.mouse.move.assert_called_once_with(17.0, 23.0)
        page.wait_for_timeout.assert_called_once_with(100)

    def test_category_item_lookup_uses_unique_path_suffix_for_short_cached_id(self):
        column = FakeCategoryDropdown(["1000002395__RC_CASCADER_SPLIT__1000007909"])
        item = browser_cdp._category_item_by_id(column, "1000007909")
        self.assertIsNotNone(item)
        self.assertEqual(item.get_attribute("data-path-key"), "1000002395__RC_CASCADER_SPLIT__1000007909")

    def test_opening_category_waits_for_target_without_fixed_settle_delay(self):
        page = MagicMock()
        page.locator.return_value.count.return_value = 0
        control = MagicMock()
        target = object()
        with patch.object(browser_cdp, "_close_category_dropdown"), \
                patch.object(browser_cdp, "_wait_category_item_present") as wait_for_target, \
                patch.object(browser_cdp, "_category_dropdown_for_item", side_effect=[None, target]) as find_dropdown:
            result = _open_category_dropdown(page, "4", control, expected_item_id="second-id")

        self.assertIs(result, target)
        wait_for_target.assert_called_once_with(page, "second-id", timeout_ms=12_000)
        self.assertEqual(find_dropdown.call_count, 2)
        control.click.assert_called_once_with(force=True, timeout=3_000, no_wait_after=True)
        page.wait_for_timeout.assert_not_called()

    def test_opening_category_rechecks_portal_after_target_first_appears(self):
        page = MagicMock()
        page.locator.return_value.count.return_value = 0
        control = MagicMock()
        target = object()
        with patch.object(browser_cdp, "_close_category_dropdown"), \
                patch.object(browser_cdp, "_wait_category_item_present") as wait_for_target, \
                patch.object(browser_cdp, "_category_dropdown_for_item", side_effect=[None, None, target]) as find_dropdown:
            result = _open_category_dropdown(page, "4", control, expected_item_id="second-id")

        self.assertIs(result, target)
        wait_for_target.assert_called_once_with(page, "second-id", timeout_ms=12_000)
        self.assertEqual(find_dropdown.call_count, 3)
        page.wait_for_timeout.assert_called_once_with(100)

    def test_opening_third_level_waits_for_stable_portal_after_expand(self):
        page = MagicMock()
        initial_dropdown = MagicMock()
        initial_columns = initial_dropdown.locator.return_value
        initial_columns.count.return_value = 1
        second_column = initial_columns.first
        second_item = second_column.locator.return_value
        second_item.count.return_value = 1
        expand = second_item.locator.return_value
        expand.count.return_value = 1
        target_dropdown = MagicMock()
        target_columns = target_dropdown.locator.return_value
        target_columns.count.return_value = 2
        target_leaf_column = target_columns.nth.return_value
        leaf = MagicMock()
        leaf.is_visible.return_value = True
        with patch.object(browser_cdp, "_category_dropdown_for_item", side_effect=[None, target_dropdown]), \
                patch.object(browser_cdp, "_category_item_by_id", side_effect=[None, leaf]), \
                patch.object(browser_cdp, "_wait_category_item_present") as wait_for_target, \
                patch.object(browser_cdp, "_hover_category_expand"):
            result_dropdown, result_leaf = browser_cdp._open_category_branch(
                page, "5", "1000004647", "1000004647__RC_CASCADER_SPLIT__1000004649", initial_dropdown
            )

        self.assertIs(result_dropdown, target_dropdown)
        self.assertIs(result_leaf, leaf)
        wait_for_target.assert_called_once_with(page, "1000004647__RC_CASCADER_SPLIT__1000004649", timeout_ms=8_000)
        page.wait_for_timeout.assert_called_once_with(100)

    def test_third_level_lazy_load_timeout_reopens_menu_and_retries_once(self):
        page = MagicMock()
        control = MagicMock()
        first_dropdown = object()
        refreshed_dropdown = object()
        leaf = object()
        target = "1000002395__RC_CASCADER_SPLIT__1000002413"
        with patch.object(browser_cdp, "_open_category_branch", side_effect=[
            RuntimeError(f"百应菜单等待 8 秒后仍未加载三级类目 ID {target}"),
            (refreshed_dropdown, leaf),
        ]) as open_branch, patch.object(
            browser_cdp, "_open_category_dropdown", return_value=refreshed_dropdown
        ) as open_dropdown:
            result = browser_cdp._open_category_branch_with_retry(
                page, "9", "1000002395", target, control, first_dropdown
            )

        self.assertEqual(result, (refreshed_dropdown, leaf))
        self.assertEqual(open_branch.call_count, 2)
        open_dropdown.assert_called_once_with(page, "9", control, expected_item_id="1000002395")
        page.wait_for_timeout.assert_called_once_with(500)

    def test_missing_second_level_reopens_menu_once_before_any_category_click(self):
        page = MagicMock()
        control = MagicMock()
        first_dropdown = object()
        refreshed_dropdown = object()
        leaf = object()
        with patch.object(browser_cdp, "_open_category_branch", side_effect=[
            RuntimeError("百应展开菜单后未找到二级类目 ID second"),
            (refreshed_dropdown, leaf),
        ]) as open_branch, patch.object(
            browser_cdp, "_open_category_dropdown", return_value=refreshed_dropdown
        ) as open_dropdown:
            result = browser_cdp._open_category_branch_with_retry(
                page, "root", "second", "leaf", control, first_dropdown,
                allow_second_level_retry=True,
            )

        self.assertEqual(result, (refreshed_dropdown, leaf))
        self.assertEqual(open_branch.call_count, 2)
        open_dropdown.assert_called_once_with(page, "root", control, expected_item_id="second")
        page.wait_for_timeout.assert_called_once_with(500)

    def test_missing_second_level_is_not_retried_after_a_selection_may_have_started(self):
        page = MagicMock()
        control = MagicMock()
        dropdown = object()
        with patch.object(browser_cdp, "_open_category_branch", side_effect=RuntimeError(
            "百应展开菜单后未找到二级类目 ID second"
        )) as open_branch, patch.object(browser_cdp, "_open_category_dropdown") as open_dropdown:
            with self.assertRaisesRegex(RuntimeError, "未找到二级类目 ID second"):
                browser_cdp._open_category_branch_with_retry(
                    page, "root", "second", "leaf", control, dropdown,
                    allow_second_level_retry=False,
                )

        open_branch.assert_called_once()
        open_dropdown.assert_not_called()
        page.wait_for_timeout.assert_not_called()

    def test_category_apply_error_contains_full_saved_path(self):
        root = {"id": "food", "name": "食品饮料", "children": [{
            "id": "grains", "name": "五谷杂粮", "children": [
                {"id": "leaf", "name": "杂粮粉"},
            ],
        }]}
        root_locator = MagicMock()
        root_locator.count.return_value = 1
        root_locator.first = root_locator
        control = MagicMock()
        root_locator.locator.return_value = control
        page = MagicMock()
        page.locator.return_value = root_locator

        with patch.object(browser_cdp, "_close_category_dropdown"), \
                patch.object(browser_cdp, "_open_category_dropdown", return_value=object()), \
                patch.object(browser_cdp, "_open_category_branch_with_retry", side_effect=RuntimeError(
                    "百应展开菜单后未找到二级类目 ID grains"
                )):
            with self.assertRaisesRegex(
                RuntimeError,
                "食品饮料（food） → 五谷杂粮（grains） → 杂粮粉（leaf）",
            ):
                apply_category_settings(page, {
                    "categorySelection": {
                        "level1": ["food"], "level2": ["grains"], "level3": ["leaf"],
                    },
                    "categoryTree": [root],
                })

    def test_filter_apply_error_reports_observed_and_pending_requests_without_searching(self):
        class Request:
            url = "https://buyin.jinritemai.com/pc/selection/common/material_list"

        page = MagicMock()
        page.evaluate.return_value = ""
        settings = {
            "categorySelection": {"level1": ["food"], "level2": ["grains"], "level3": ["leaf"]},
            "categoryTree": [],
        }
        collectors = []

        def capture_collector(_page, collector):
            collectors.append(collector)

        def fail_after_filter_request(*_args, **_kwargs):
            collectors[0].on_request(Request())
            raise RuntimeError("百应类目路径展开失败")

        with patch.object(browser_cdp, "_read_search_keyword", return_value=""), \
                patch.object(browser_cdp, "_configured_search_keyword", return_value=""), \
                patch.object(browser_cdp, "_get_cached_material_list", return_value=None), \
                patch.object(browser_cdp, "_page_rate_limit_remaining", return_value=0), \
                patch.object(browser_cdp, "_listen_to_material_list", side_effect=capture_collector), \
                patch.object(browser_cdp, "_remove_material_list_listeners"), \
                patch.object(browser_cdp, "apply_filter_settings", side_effect=fail_after_filter_request), \
                patch.object(browser_cdp, "search_baiying_candidates") as search:
            with self.assertRaisesRegex(
                RuntimeError,
                "已观察到 1 个 material_list 请求，其中 1 个尚未结束；未继续执行候选搜索",
            ):
                browser_cdp._apply_one_category_and_collect_candidates(page, settings)

        search.assert_not_called()

    def test_filter_apply_11001_sets_cooldown_even_when_category_application_raises(self):
        page = MagicMock()
        page.evaluate.return_value = ""
        settings = {
            "categorySelection": {"level1": ["food"], "level2": ["grains"], "level3": ["leaf"]},
            "categoryTree": [],
        }
        collectors = []

        def capture_collector(_page, collector):
            collectors.append(collector)

        def fail_with_rate_limit(*_args, **_kwargs):
            collectors[0].errors.append((1, "11001", "限流响应"))
            raise RuntimeError("百应类目路径展开失败")

        with patch.object(browser_cdp, "_read_search_keyword", return_value=""), \
                patch.object(browser_cdp, "_configured_search_keyword", return_value=""), \
                patch.object(browser_cdp, "_get_cached_material_list", return_value=None), \
                patch.object(browser_cdp, "_page_rate_limit_remaining", return_value=0), \
                patch.object(browser_cdp, "_listen_to_material_list", side_effect=capture_collector), \
                patch.object(browser_cdp, "_remove_material_list_listeners"), \
                patch.object(browser_cdp, "apply_filter_settings", side_effect=fail_with_rate_limit), \
                patch.object(browser_cdp, "_mark_page_rate_limited") as mark_cooldown, \
                patch.object(browser_cdp, "search_baiying_candidates") as search:
            with self.assertRaisesRegex(RuntimeError, "返回 11001.*进入 .* 秒冷却.*未继续执行候选搜索"):
                browser_cdp._apply_one_category_and_collect_candidates(page, settings)

        mark_cooldown.assert_called_once_with(id(page))
        search.assert_not_called()

    def test_opening_category_reuses_visible_target_menu_without_toggling_it(self):
        page = MagicMock()
        control = MagicMock()
        target = object()
        with patch.object(browser_cdp, "_category_dropdown_for_item", return_value=target) as find_dropdown, \
                patch.object(browser_cdp, "_close_category_dropdown") as close_dropdown:
            result = _open_category_dropdown(page, "4", control, expected_item_id="second-id")

        self.assertIs(result, target)
        find_dropdown.assert_called_once_with(page, "second-id")
        close_dropdown.assert_not_called()
        control.click.assert_not_called()

    def test_category_close_waits_for_leave_animation_instead_of_reclicking_control(self):
        page = MagicMock()
        page.locator.return_value.count.return_value = 1
        control = MagicMock()
        with patch.object(browser_cdp, "_wait_category_dropdowns_hidden") as wait_hidden:
            browser_cdp._close_category_dropdown(page, "root", control)

        page.keyboard.press.assert_called_once_with("Escape")
        wait_hidden.assert_called_once_with(page, timeout_ms=1_200)
        control.click.assert_not_called()

    def test_category_click_retries_once_only_when_leaf_remains_unchecked(self):
        class Leaf:
            checked = False

            def get_attribute(self, name):
                return "true" if name == "aria-checked" and self.checked else "false"

            def inner_text(self):
                return "奢品彩妆"

        page = MagicMock()
        control = MagicMock()
        menu = object()
        leaf = Leaf()
        click_count = [0]

        def click_once_or_recover(_item):
            click_count[0] += 1
            # 模拟百应首击丢失，只有安全补点才真正勾选。
            if click_count[0] == 2:
                leaf.checked = True

        def wait_checked(_page, _category_id, timeout_ms=4_000):
            if not leaf.checked:
                raise browser_cdp.PlaywrightTimeoutError("类目仍未勾选")

        with patch.object(browser_cdp, "_click_category_item", side_effect=click_once_or_recover), \
                patch.object(browser_cdp, "_open_category_dropdown", return_value=menu), \
                patch.object(browser_cdp, "_open_category_branch", return_value=(menu, leaf)), \
                patch.object(browser_cdp, "_wait_category_item_checked", side_effect=wait_checked):
            result_menu, result_leaf = _click_category_and_confirm(
                page, "root", "second", "third", control, menu, leaf
            )

        self.assertIs(result_menu, menu)
        self.assertIs(result_leaf, leaf)
        self.assertEqual(click_count[0], 2)
        self.assertTrue(leaf.checked)

    def test_platform_category_root_clear_uses_not_limited_action_and_verifies(self):
        page = MagicMock()
        root = page.locator.return_value
        root.count.return_value = 1
        root.first = root
        control = root.locator.return_value
        dropdown = MagicMock()
        first_column = MagicMock()
        dropdown.locator.return_value.first = first_column
        clear_item = MagicMock()
        clear_items = first_column.locator.return_value
        clear_items.count.return_value = 1
        clear_items.first = clear_item
        with patch.object(browser_cdp, "_open_category_dropdown", return_value=dropdown) as open_dropdown, \
                patch.object(browser_cdp, "_click_category_item") as click, \
                patch.object(browser_cdp, "_wait_platform_categories_unchecked") as wait_clear, \
                patch.object(browser_cdp, "_close_category_dropdown"):
            browser_cdp._clear_platform_category_root(page, "root", {
                "second-id", "second-id__RC_CASCADER_SPLIT__leaf-id"
            })

        open_dropdown.assert_called_once_with(
            page, "root", control, expected_item_id="second-id"
        )
        wait_clear.assert_called_once_with(
            page, {"second-id", "second-id__RC_CASCADER_SPLIT__leaf-id"}, timeout_ms=5_000
        )
        click.assert_called_once_with(clear_item)

    def test_category_root_clear_wait_checks_global_portal_state(self):
        page = MagicMock()
        browser_cdp._wait_platform_categories_unchecked(page, {"leaf-b", "leaf-a"}, timeout_ms=2_500)

        args, kwargs = page.wait_for_function.call_args
        self.assertIn('aria-checked="true"', args[0])
        self.assertEqual(kwargs["arg"], ["leaf-a", "leaf-b"])
        self.assertEqual(kwargs["timeout"], 2_500)

    def test_category_click_does_not_toggle_a_leaf_that_became_checked(self):
        class Leaf:
            def get_attribute(self, name):
                return "true" if name == "aria-checked" else None

        page = MagicMock()
        control = MagicMock()
        menu = object()
        leaf = Leaf()
        with patch.object(browser_cdp, "_click_category_item") as click, \
                patch.object(browser_cdp, "_open_category_dropdown") as reopen:
            result_menu, result_leaf = _click_category_and_confirm(
                page, "root", "second", "third", control, menu, leaf
            )

        self.assertIs(result_menu, menu)
        self.assertIs(result_leaf, leaf)
        click.assert_not_called()
        reopen.assert_not_called()

    def test_category_application_reuses_verified_menu_for_next_leaf(self):
        class SelectableItem:
            def __init__(self):
                self.checked = False

            def get_attribute(self, name):
                return "true" if name == "aria-checked" and self.checked else "false"

            def locator(self, _selector):
                return self

            def click(self, **_kwargs):
                self.checked = True

        root = {"id": "root", "name": "食品", "children": [{
            "id": "second", "name": "休闲食品", "children": [
                {"id": f"third-{index}", "name": f"末级{index}"} for index in range(3)
            ]
        }]}
        root_locator = MagicMock()
        root_locator.count.return_value = 1
        root_locator.first = root_locator
        root_locator.locator.return_value = MagicMock()
        page = MagicMock()
        page.locator.return_value = root_locator
        items = {f"third-{index}": SelectableItem() for index in range(3)}
        dropdowns = [object() for _ in range(4)]

        with patch.object(browser_cdp, "_close_category_dropdown"), \
                patch.object(browser_cdp, "_open_category_dropdown", side_effect=dropdowns) as open_dropdown, \
                patch.object(browser_cdp, "_open_category_branch", side_effect=lambda _page, _root, _second, item_id, menu: (menu, items[item_id])), \
                patch.object(browser_cdp, "_wait_category_item_checked"):
            warnings = apply_category_settings(page, {
                "categorySelection": {"level1": ["root"], "level2": ["second"],
                                      "level3": ["third-0", "third-1", "third-2"]},
                "categoryTree": [root],
            })

        self.assertEqual(warnings, [])
        self.assertEqual(open_dropdown.call_count, 4)
        self.assertTrue(all(item.checked for item in items.values()))

    def test_rendered_material_payload_reads_current_card_models(self):
        fake_page = FakePage({})
        rows = [{"product_id": "rendered-product", "base_model": {
            "product_info": {"name": "页面当前商品"}
        }}]
        with patch.object(fake_page, "evaluate", return_value=rows):
            payload = browser_cdp._rendered_material_payload(fake_page)
        self.assertEqual(payload["data"]["summary_promotions"], rows)

    def test_candidate_card_locator_reads_react_product_ids(self):
        page = FakePage({"商品标题": [FakeCard("<div class='card'></div>", react_ids={"product-b"})]})
        _title, card = _locate_candidate_card(page, {
            "title": "商品标题", "productId": "product-b", "commodityId": "promotion-b"
        })
        self.assertIn("product-b", card.react_ids)

    def test_filter_application_falls_back_to_search_when_no_response_is_captured(self):
        fake_page = FakePage({})
        settings = {"candidateLimit": 1}
        clock = [0.0]
        def tick():
            clock[0] += 2.0
            return clock[0]
        expected = {"ok": True, "candidateCount": 1, "candidates": [
            {"cardIndex": 3, "productId": "final-search-product", "title": "最终搜索商品"}
        ], "warnings": []}
        with patch.object(browser_cdp, "apply_filter_settings", return_value=["筛选已应用"]) as apply, \
                patch.object(browser_cdp, "search_baiying_candidates", return_value=expected) as search, \
                patch.object(browser_cdp.time, "time", side_effect=tick):
            result = browser_cdp.apply_settings_and_collect_candidates(
                fake_page, settings, force_refresh=True, preserve_product_ids=["original-product"]
            )

        apply.assert_called_once_with(fake_page, settings)
        search.assert_called_once_with(fake_page, settings, ["original-product"])
        self.assertEqual(result["candidates"], expected["candidates"])
        self.assertEqual(result["warnings"], ["筛选已应用"])

    def test_filter_application_uses_last_response_after_intermediate_lists(self):
        fake_page = FakePage({})
        settings = {"candidateLimit": 1, "poolFallback": True, "poolPriority": []}
        clock = [0.0]
        def tick():
            clock[0] += 2.0
            return clock[0]
        intermediate = {"data": {"summary_promotions": [{
            "product_id": "intermediate-product", "base_model": {"product_info": {"name": "中间列表商品"}}
        }]}}
        final = {"data": {"summary_promotions": [{
            "product_id": "final-product", "base_model": {"product_info": {"name": "最终列表商品"}}
        }]}}

        def apply_and_emit(page, _settings):
            for response in (FakeResponse(intermediate), FakeResponse(final)):
                for callback in page.listeners.get("response", []):
                    callback(response)
            return []

        with patch.object(browser_cdp, "apply_filter_settings", side_effect=apply_and_emit), \
                patch.object(browser_cdp, "search_baiying_candidates") as search, \
                patch.object(browser_cdp.time, "time", side_effect=tick):
            result = browser_cdp.apply_settings_and_collect_candidates(fake_page, settings, force_refresh=True)

        self.assertEqual([row["productId"] for row in result["candidates"]], ["final-product"])
        search.assert_not_called()

    def test_recovery_refresh_preserves_original_product_outside_candidate_limit(self):
        payload = {
            "data": {
                "summary_promotions": [
                    {"product_id": "product-top", "base_model": {
                        "product_info": {"name": "当前排名第一", "month_sale": 1000}
                    }},
                    {"product_id": "product-original", "base_model": {
                        "product_info": {"name": "本批已选商品", "month_sale": 10}
                    }},
                ]
            }
        }
        settings = {"candidateLimit": 1, "poolFallback": True, "poolPriority": []}

        normal = browser_cdp._candidate_result(payload, settings)
        refreshed = browser_cdp._candidate_result(
            payload, settings, preserve_product_ids=["product-original"]
        )

        self.assertEqual([row["productId"] for row in normal["candidates"]], ["product-top"])
        self.assertEqual(
            [row["productId"] for row in refreshed["candidates"]],
            ["product-top", "product-original"],
        )

    def test_saved_selected_ids_map_to_new_card_indexes(self):
        old_candidates = [
            {"cardIndex": 8, "productId": "product-a"},
            {"cardIndex": 4, "productId": "product-b"},
        ]
        ids = selected_product_ids(old_candidates, [4])
        self.assertEqual(ids, ["product-b"])

        refreshed_candidates = [
            {"cardIndex": 0, "productId": "product-b"},
            {"cardIndex": 1, "productId": "product-c"},
        ]
        indexes, missing = remap_selected_candidate_indexes(refreshed_candidates, ids)
        self.assertEqual(indexes, [0])
        self.assertEqual(missing, [])

    def test_missing_selected_product_is_reported_without_substitution(self):
        indexes, missing = remap_selected_candidate_indexes(
            [{"cardIndex": 0, "productId": "different-product"}], ["original-product"]
        )
        self.assertEqual(indexes, [])
        self.assertEqual(missing, ["original-product"])

    def test_duplicate_title_is_disambiguated_by_product_id(self):
        page = FakePage({"同名商品": [
            FakeCard('<div data-product-id="product-a"></div>'),
            FakeCard('<div data-product-id="product-b"></div>'),
        ]})
        _title, card = _locate_candidate_card(page, {
            "title": "同名商品", "productId": "product-b", "commodityId": "promotion-b"
        })
        self.assertIn("product-b", card.html)

    def test_ambiguous_duplicate_title_fails_closed_before_add_cart(self):
        page = FakePage({"同名商品": [
            FakeCard("<div class='card'>第一张</div>"),
            FakeCard("<div class='card'>第二张</div>"),
        ]})
        with self.assertRaisesRegex(RuntimeError, "无法按商品 ID 唯一定位"):
            _locate_candidate_card(page, {
                "title": "同名商品", "productId": "product-b", "commodityId": "promotion-b"
            })

    def test_run_command_refreshes_missing_cards_and_executes_by_original_product_id(self):
        old_candidates = [
            {"cardIndex": 8, "productId": "product-a", "title": "商品 A"},
            {"cardIndex": 4, "productId": "product-b", "title": "商品 B"},
        ]
        new_candidates = [
            {"cardIndex": 0, "productId": "product-b", "title": "商品 B"},
            {"cardIndex": 1, "productId": "product-c", "title": "商品 C"},
        ]
        fake_page = FakePage({})
        command = {
            "settings": {"candidateLimit": 1},
            "candidateSnapshot": old_candidates,
            "selectedIndexes": [4],
            "downloadPath": "D:/downloads/test",
            "previewOnly": False,
        }
        with patch.object(browser_cdp, "apply_settings_and_collect_candidates", return_value={
            "ok": True, "candidates": new_candidates, "warnings": []
        }) as refresh, patch.object(browser_cdp, "_candidate_card_is_available", side_effect=[False, True]), \
                patch.object(browser_cdp, "execute_selected_candidates", return_value=[{
                    "productId": "product-b", "cartAdded": True
                }]) as execute:
            result = run_selection_command(fake_page, object(), command)

        refresh.assert_called_once_with(
            fake_page,
            command["settings"],
            force_refresh=True,
            preserve_product_ids=["product-b"],
        )
        self.assertEqual(result["completed"][0]["productId"], "product-b")
        self.assertEqual(execute.call_args.args[2], new_candidates)
        self.assertEqual(execute.call_args.args[3], [0])

    def test_recovery_refreshes_cart_state_even_when_old_card_is_still_visible(self):
        old_candidates = [{"cardIndex": 4, "productId": "product-b", "title": "商品 B", "inCart": False}]
        new_candidates = [{"cardIndex": 0, "productId": "product-b", "title": "商品 B", "inCart": True}]
        fake_page = FakePage({"商品 B": [FakeCard('<div data-product-id="product-b"></div>')]})
        command = {
            "settings": {"candidateLimit": 1},
            "candidateSnapshot": old_candidates,
            "selectedIndexes": [4],
            "refreshCandidatesById": True,
            "downloadPath": "D:/downloads/test",
            "previewOnly": False,
        }
        with patch.object(browser_cdp, "apply_settings_and_collect_candidates", return_value={
            "ok": True, "candidates": new_candidates, "warnings": []
        }) as refresh, patch.object(browser_cdp, "execute_selected_candidates", return_value=[{
            "productId": "product-b", "cartAdded": True, "cartStatus": "已在选品车"
        }]) as execute:
            result = run_selection_command(fake_page, object(), command)

        refresh.assert_called_once_with(
            fake_page,
            command["settings"],
            force_refresh=True,
            preserve_product_ids=["product-b"],
        )
        self.assertEqual(execute.call_args.args[2], new_candidates)
        self.assertEqual(execute.call_args.args[3], [0])
        self.assertTrue(execute.call_args.args[2][0]["inCart"])
        self.assertEqual(result["completed"][0]["cartStatus"], "已在选品车")

    def test_run_command_stops_if_original_product_disappeared(self):
        old_candidates = [{"cardIndex": 4, "productId": "original-product", "title": "原商品"}]
        fake_page = FakePage({})
        command = {
            "settings": {"candidateLimit": 1},
            "candidateSnapshot": old_candidates,
            "selectedIndexes": [4],
            "downloadPath": "D:/downloads/test",
            "previewOnly": False,
        }
        with patch.object(browser_cdp, "apply_settings_and_collect_candidates", return_value={
            "ok": True, "candidates": [{"cardIndex": 0, "productId": "different-product", "title": "别的商品"}],
            "warnings": []
        }), patch.object(browser_cdp, "search_baiying_candidates", return_value={
            "ok": True, "candidates": [{"cardIndex": 0, "productId": "different-product", "title": "别的商品"}],
            "warnings": []
        }) as exact_search, patch.object(browser_cdp, "execute_selected_candidates") as execute:
            with self.assertRaisesRegex(RuntimeError, "为避免误加其他商品"):
                run_selection_command(fake_page, object(), command)
        exact_search.assert_called_once_with(fake_page, command["settings"], preserve_product_ids=["original-product"])
        execute.assert_not_called()

    def test_run_command_retries_exact_search_before_stopping_for_missing_id(self):
        old_candidates = [{"cardIndex": 4, "productId": "original-product", "title": "原商品"}]
        refreshed_candidates = [{"cardIndex": 0, "productId": "different-product", "title": "其他商品"}]
        searched_candidates = [{"cardIndex": 7, "productId": "original-product", "title": "原商品"}]
        fake_page = FakePage({})
        command = {
            "settings": {"candidateLimit": 1},
            "candidateSnapshot": old_candidates,
            "selectedIndexes": [4],
            "downloadPath": "D:/downloads/test",
            "previewOnly": False,
        }
        with patch.object(browser_cdp, "apply_settings_and_collect_candidates", return_value={
            "ok": True, "candidates": refreshed_candidates, "warnings": []
        }), patch.object(browser_cdp, "search_baiying_candidates", return_value={
            "ok": True, "candidates": searched_candidates, "warnings": []
        }) as exact_search, patch.object(browser_cdp, "_candidate_card_is_available", side_effect=[False, True]), \
                patch.object(browser_cdp, "execute_selected_candidates", return_value=[{
                    "productId": "original-product", "cartAdded": True
                }]) as execute:
            result = run_selection_command(fake_page, object(), command)

        exact_search.assert_called_once_with(fake_page, command["settings"], preserve_product_ids=["original-product"])
        self.assertEqual(result["completed"][0]["productId"], "original-product")
        self.assertEqual(execute.call_args.args[2], searched_candidates)
        self.assertEqual(execute.call_args.args[3], [7])


    def test_baiying_cart_row_selection_prefers_exact_product_id(self):
        unrelated = {"locator": "unrelated", "matchesProductId": False, "matchesProductTitle": False}
        target = {"locator": "target", "matchesProductId": True, "matchesProductTitle": True}
        duplicate_title = {"locator": "duplicate-title", "matchesProductId": False, "matchesProductTitle": True}

        self.assertIs(_select_baiying_product_row([unrelated, duplicate_title, target]), target)

    def test_baiying_cart_row_selection_uses_unique_title_when_id_is_not_exposed(self):
        target = {"locator": "target", "matchesProductId": False, "matchesProductTitle": True}
        unrelated = {"locator": "unrelated", "matchesProductId": False, "matchesProductTitle": False}

        self.assertIs(_select_baiying_product_row([unrelated, target]), target)

    def test_baiying_cart_row_selection_rejects_duplicate_product_id(self):
        matches = [
            {"matchesProductId": True, "matchesProductTitle": True},
            {"matchesProductId": True, "matchesProductTitle": False},
        ]
        with self.assertRaisesRegex(RuntimeError, "商品 ID 命中 2 个"):
            _select_baiying_product_row(matches)

    def test_baiying_row_locator_scans_nested_nodes_for_product_identity(self):
        row = MagicMock()
        row.evaluate.return_value = {
            "matchesProductId": True,
            "matchesProductTitle": False,
            "rowKey": "row-7",
            "rowText": "目标商品标题…",
            "identityHints": ["goodsId=3798361431993352595"],
            "descendantCount": 6,
            "reactObjectsVisited": 12,
        }
        rows = MagicMock()
        rows.count.return_value = 1
        rows.nth.return_value = row
        page = MagicMock()
        page.locator.return_value = rows

        with patch.object(browser_cdp, "_is_visible", return_value=True):
            selected, details = _locate_baiying_product_row(page, "3798361431993352595", "完整商品标题")

        self.assertIs(selected, row)
        self.assertIn("按商品 ID命中 1 行", details)
        self.assertIn("querySelectorAll", BAIYING_ROW_METADATA_SCRIPT)
        self.assertIn("__reactProps$", BAIYING_ROW_METADATA_SCRIPT)
        self.assertEqual(row.evaluate.call_args.args[1]["productId"], "3798361431993352595")

    def test_baiying_row_locator_wrong_visible_id_skips_candidate(self):
        row = MagicMock()
        row.evaluate.return_value = {
            "matchesProductId": False,
            "matchesProductTitle": False,
            "rowKey": "row-12",
            "rowText": "另一件商品的可见标题",
            "identityHints": ["goodsId=987654321"],
        }
        visible_rows = MagicMock()
        visible_rows.count.return_value = 1
        visible_rows.nth.return_value = row
        empty_rows = MagicMock()
        empty_rows.count.return_value = 0
        page = MagicMock()
        page.locator.side_effect = lambda selector: visible_rows if selector == "tbody tr" else empty_rows

        with patch.object(browser_cdp, "_is_visible", return_value=True), self.assertRaises(
            CandidateCartSkip
        ) as error:
            _locate_baiying_product_row(page, "3798361431993352595", "完整商品标题")

        self.assertIn("3798361431993352595", str(error.exception))
        self.assertIn("其他商品行", str(error.exception))
        self.assertIn("987654321", str(error.exception))

    def test_baiying_row_snapshot_reports_visible_count_and_bounded_sample(self):
        row = MagicMock()
        row.evaluate.return_value = {
            "rowKey": "row-1",
            "rowText": "查询前的商品标题",
            "identityHints": ["goodsId=123"],
        }
        rows = MagicMock()
        rows.count.return_value = 4
        rows.nth.return_value = row
        page = MagicMock()
        page.locator.return_value = rows

        with patch.object(browser_cdp, "_is_visible", return_value=True):
            snapshot = _baiying_row_snapshot(page, max_rows=1)

        self.assertIn("tbody tr:行4", snapshot)
        self.assertIn("row-1", snapshot)
        self.assertIn("查询前的商品标题", snapshot)
        self.assertEqual(row.evaluate.call_count, 1)

    def test_baiying_cart_click_is_scoped_to_selected_row(self):
        action = MagicMock()
        submenu = MagicMock()
        page = MagicMock()
        row = MagicMock()
        with patch.object(browser_cdp, "_visible_exact_text", side_effect=lambda scope, label: (
            [action] if scope is row and label == "去带货" else [submenu]
        )):
            _open_baiying_product_content_menu(page, row, "按商品 ID 命中 1 行")

        action.hover.assert_called_once_with(timeout=5_000)
        submenu.click.assert_called_once_with(timeout=5_000)

    def test_baiying_cart_click_rejects_multiple_actions_inside_target_row(self):
        first_action = MagicMock()
        second_action = MagicMock()
        page = MagicMock()
        row = MagicMock()
        with patch.object(browser_cdp, "_visible_exact_text", return_value=[first_action, second_action]):
            with self.assertRaisesRegex(RuntimeError, "行内“去带货”操作匹配 2 个"):
                _open_baiying_product_content_menu(page, row, "按商品 ID 命中 1 行")

        first_action.hover.assert_not_called()
        second_action.hover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
