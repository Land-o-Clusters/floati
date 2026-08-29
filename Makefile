.PHONY: demo demo-capture wall-capture

demo:
	@python3 -m floati.demo

demo-capture:
	@mkdir -p docs/evidence/captures
	@python3 -m floati.demo --capture color > docs/evidence/captures/hm1-tui-color.txt
	@python3 -m floati.demo --capture monochrome > docs/evidence/captures/hm1-tui-monochrome.txt

wall-capture:
	@python3 scripts/capture-wall.py
