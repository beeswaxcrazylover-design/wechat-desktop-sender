import _support
import importlib.util
import unittest

from wechat_desktop_sender.search_once import unique_chat_entry, opened_remark_matches, search_layout
from wechat_desktop_sender.live_capture_once import Stop

HAS_PIXELS=all(importlib.util.find_spec(x) for x in ("PIL","numpy","cv2"))


@unittest.skipUnless(HAS_PIXELS,"optional desktop image dependencies are absent")
class VisionTests(unittest.TestCase):
    def canvas(self, ys=(60,)):
        from PIL import Image,ImageDraw
        image=Image.new("RGB",(400,300),(240,240,240))
        draw=ImageDraw.Draw(image)
        for y in ys:draw.rectangle((30,y,69,y+39),fill=(30,70,120))
        self.addCleanup(image.close)
        return image
    def row(self,y=70,text="731",score=.99,complete=True):
        return {"text":text,"box":[90,y,130,y+20],"score":score,"complete":complete}
    def test_unique_avatar_row_needs_no_group_or_frequent_category(self):
        found=unique_chat_entry([self.row()],self.canvas(),"731")
        self.assertEqual(found["kind"],"chat_entry")
        self.assertFalse(found["category_required"])
    def test_bare_text_without_avatar_is_rejected(self):
        with self.assertRaises(Stop):unique_chat_entry([self.row()],self.canvas(()),"731")
    def test_duplicate_ocr_same_rect_is_merged(self):
        self.assertEqual(unique_chat_entry([self.row(),self.row(71)],self.canvas(),"731")["visible_exact_match_count"],1)
    def test_two_chat_rows_are_ambiguous(self):
        with self.assertRaises(Stop):unique_chat_entry([self.row(),self.row(150)],self.canvas((60,140)),"731")
    def test_low_confidence_or_clipped_exact_alias_rejected(self):
        for row in (self.row(score=.8),self.row(complete=False)):
            with self.subTest(row=row),self.assertRaises(Stop):unique_chat_entry([row],self.canvas(),"731")
    def test_message_history_occurrence_is_excluded(self):
        heading={"text":"聊天记录","box":[10,40,90,55],"score":.99,"complete":True}
        with self.assertRaises(Stop):unique_chat_entry([heading,self.row()],self.canvas(),"731")
    def test_network_lookup_and_later_results_are_excluded(self):
        heading={"text":"网络查找手机/QQ号","box":[10,40,180,55],"score":.99,"complete":True}
        with self.assertRaises(Stop):unique_chat_entry([heading,self.row()],self.canvas(),"731")
    def test_substring_is_not_exact_target(self):
        with self.assertRaises(Stop):unique_chat_entry([self.row(text="731-extra")],self.canvas(),"731")


class TitleTests(unittest.TestCase):
    def test_complete_exact_remark_and_group_count(self):
        for text in ("731","731 (12)","731（12）"):
            self.assertTrue(opened_remark_matches([{"text":text,"score":.99,"complete":True}],"731"))
        for text in ("1731","731 extra","846"):
            self.assertFalse(opened_remark_matches([{"text":text,"score":.99,"complete":True}],"731"))

