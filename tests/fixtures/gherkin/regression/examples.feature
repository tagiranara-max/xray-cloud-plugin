Feature: Examples blocks keep names, descriptions and tags

  @outline-tag
  Scenario Outline: Several examples
    Given <a> and <b>

    @smoke @fast
    Examples: First block
      This block has a description
      over two lines.

      | a | b |
      | 1 | 2 |

    @slow
    Examples: Second block
      | a | b |
      | 3 | 4 |
      | 5 | 6 |

    Examples:
      | a | b |
      | 7 | 8 |

  Scenario Template: Template keyword
    Given <c>

    Scenarios: Alt keyword
      | c |
      | 9 |
