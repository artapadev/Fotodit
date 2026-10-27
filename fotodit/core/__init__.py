"""Fotodit's core: reading and removing metadata (`meta`), converting and editing photos and videos
(`processor`, `edits`, `rawdev`, `video`), planning names and folders (`planner`) and applying the
plan to disk (`runner`). It never imports Qt, so it runs and is tested without a user interface; the
Windows helpers it uses live in `fotodit.winutil`.
"""
