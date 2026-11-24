@suite=regression @team
Feature: Tagged checkout

  @area:ui
  Scenario: Checkout by card
    Given a card

  @areas=api,users @comp=backend
  Rule: Accounts
    @wip
    Scenario: Create account
      Given an account form

    Scenario: Delete account
      Given an account
