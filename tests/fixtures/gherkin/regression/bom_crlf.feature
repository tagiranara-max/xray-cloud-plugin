Feature: BOM, CRLF, tabs and non-ASCII

  Scenario: Grüße	mit Tab
    Given a step with	a tab and ünïcödé ✓
      | col	A | 日本 |
      | x	y   | ü    |
    Then a doc string:
      """
      	leading tab
      émoji 🙂
      """
