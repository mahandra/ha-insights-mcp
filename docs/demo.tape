# README demo GIF. Render with VHS (https://github.com/charmbracelet/vhs):
#   make dev-ha-up && make dev-simulate    # dev HA with the simulated problems
#   vhs docs/demo.tape                     # writes docs/demo.gif
Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1300
Set Height 760
Set Padding 18
Set TypingSpeed 40ms
Set Theme "Catppuccin Mocha"

Hide
Type "clear"
Enter
Show

Type "# HA Insights MCP: diagnostics + guarded actions (a real MCP client over stdio)"
Sleep 800ms
Enter
Type "uv run python dev/demo.py"
Sleep 500ms
Enter

# Diagnostics, the refused thermostat value and the confirmation prompt.
Wait+Screen@60s /Allow\?/
Sleep 3.5s
Type "y"
Sleep 600ms
Enter

Wait+Screen@30s /by=local/
Sleep 4s
