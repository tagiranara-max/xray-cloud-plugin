# Edge cases for the xray-cloud plugin's feature import (create_tests_from_feature).
# Every tag and construct below hit a problem or a design question during development.
# Expected behaviour is noted next to each case. Run it against a test project (e.g. P).

# Feature-level tags apply to every scenario:
#   @smoke             plain tag          -> "tagsAsLabels?" question (label "smoke")
#   @suite:regression  key:value          -> "what does `suite` mean?" (once per key, nothing pre-selected)
#   @team=checkout     key=value          -> second key question: "what does `team` mean?"
@smoke @suite:regression @team=checkout
Feature: Account security edge cases
  Free text under the Feature line becomes the description of the feature's Test Set.
  It spans two lines on purpose.

  # Background -> the "backgrounds" question (Pre-Conditions or inline steps).
  # Pre-Conditions are created once and linked to every test of this feature.
  Background:
    Given the user "alice" exists
    And the user is signed in

  # Plain scenario with its own description: the description must win over any
  # remembered or asked value for the Description field.
  @priority:high
  Scenario: Change password
    Each test keeps its own description, never a remembered one.

    When the user changes the password to "N3w-pass!"
    Then the user is asked to sign in again

  # Same key as the feature tag, other form and other case:
  #   @Suite=smoke  -> covered by the same `suite` question (one question, not two)
  @Suite=smoke
  Scenario: Sign out everywhere
    When the user signs out of all devices
    Then every session ends

  # Data table and doc string: the steps must arrive intact.
  Scenario: Devices I signed in from
    Given these sign-ins exist:
      | device  | city    | when       |
      | laptop  | Berlin  | 2026-11-20 |
      | phone   | Lisbon  | 2026-11-24 |
    When the user opens "Devices"
    Then the page shows:
      """
      laptop (Berlin) - 2026-11-20
      phone (Lisbon) - 2026-11-24
      """

  # Scenario Outline: rows with substituted names may not match existing test summaries
  # by name; the import must not create one test per row by accident.
  # Tags on an Examples block apply only to its rows.
  @suite:smoke
  Scenario Outline: Password rules for <length> characters
    When the user tries the password "<password>"
    Then the result is "<result>"

    @boundary
    Examples: Too short
      | length | password | result   |
      | 7      | abc1234  | rejected |

    Examples: Long enough
      | length | password  | result   |
      | 8      | abcd1234  | accepted |
      | 64     | aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa12 | accepted |

  # To test the "existing test" path, put the key of a test you already imported here,
  # e.g. @TEST_P-853 (uncomment and adjust). Adding a new key:value tag to it must ask
  # first, not label it silently.
  # @TEST_P-853 @suite:nightly
  Scenario: Remember this device
    When the user ticks "Remember this device"
    Then the next sign-in on this device skips two-factor

  # Rule with its own tags and its own Background (a second Pre-Condition, rule scope).
  @area:mfa
  Rule: Two-factor sign-in
    Background:
      Given two-factor sign-in is enabled

    # Same scenario name as in the next Rule: the import must keep them apart.
    Scenario: Sign in
      When the user enters a valid one-time code
      Then the user is signed in

    # A tag that only LOOKS like an issue key: it must not be treated as a requirement
    # link that blocks or breaks the import.
    @ISO-8601
    Scenario: Code expiry time is shown in ISO format
      When a code is sent
      Then the expiry time is shown as "2026-11-25T10:00:00Z"

  Rule: Passwordless sign-in
    Scenario: Sign in
      When the user opens the magic link
      Then the user is signed in
