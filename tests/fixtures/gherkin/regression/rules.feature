@feature
Feature: Rules keep their scenarios

  Background: Feature background
    Given the feature background

  Scenario: Top-level scenario
    Given a top-level step

  @rule-tag
  Rule: First rule
    The rule description

    Background: Rule background
      Given the rule background
        | k | v |
        | 1 | 2 |

    @scenario-tag
    Example: Inside first rule
      Given inside the first rule

    Scenario Outline: Outline inside first rule
      Given <x>

      Examples:
        | x |
        | 1 |

  Rule: Second rule without background
    Scenario: Inside second rule
      Given inside the second rule
