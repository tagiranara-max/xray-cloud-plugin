@feature-tag
Feature: Data tables everywhere

  Background:
    Given the following accounts exist:
      | name  | balance |
      | alice | 10      |
      | bob   | 20      |

  Scenario: Tables on every step keyword
    Given a given table:
      | g1 | g2 |
      | ga | gb |
    When a when table:
      | w1 |
      | wa |
    Then a then table:
      | t1 | t2 | t3 |
      | ta | tb | tc |
    And an and table:
      | a1 |
      | aa |
    But a but table:
      | b1 |
      | bb |
    * a star table:
      | s1 | s2 |
      | sa | sb |

  Scenario: Cells with escapes, empty cells and unicode
    Given a tricky table:
      | pipe   | newline    | backslash | empty | unicode     |
      | a \| b | line1\nline2 | c:\\dir   |       | Zürich ✓ 日本 |
      | \|     | \n         | \\        |       | ß           |
      | x\\\|y | end\\      |           | ""    | 🙂          |

  Scenario Outline: Tables inside outline steps
    Given user <user> has items:
      | item   | qty   |
      | <item> | <qty> |
    Then the total is <total>

    Examples:
      | user  | item  | qty | total |
      | alice | apple | 2   | 4     |
      | bob   | pear  | 1   | 3     |
