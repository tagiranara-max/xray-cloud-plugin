Feature: Inline backgrounds

  Background: Shared setup
    Given these accounts exist:
      | name  | role  |
      | alice | admin |
      | bob   | a\|b  |
    And the config is:
      """json
      {"mode": "strict"}

        indented line
      """

  Scenario: Top level
    When I log in as:
      | user  |
      | alice |

  Rule: Billing
    Background: Billing setup
      Given the invoice template:
        ```
        Line one
        Line """ two
        ```
      And the price table:
        | item | price |
        | x    | 2     |

    Scenario Outline: Inside billing
      When I buy <item>
      Then I checkout <price>

      Examples:
        | item | price |
        | x    | 2     |
