Feature: Login

  Background:
    Given the app is running

  Scenario: Successful login
    Given the following users exist:
      | username | password |
      | admin    | secret   |
    When I enter valid credentials
    Then I am logged in
    And he should see the import mode description:
      """
      The bulk limits import is used for both initial uploads and maintaining the limits master data table.
      It will update limits for the same system IDs, create new ones for new System IDs, and delete limits for missing System IDs from the imported file.
      """

  Scenario Outline: Failed login
    When I enter "<username>" and "<password>"
    Then I see error "<message>"

    Examples:
      | username | password | message          |
      | admin    | wrong    | Invalid password |
      | unknown  | pass123  | User not found   |
