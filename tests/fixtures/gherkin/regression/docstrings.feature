Feature: Doc strings keep every line

  Scenario: Multi-line doc string with blank lines and indentation
    Given a document:
      """
      first line

      third line after a blank line
        indented by two
          indented by four


      after two blank lines
      """
    And a json request_data:
      ```json
      {
        "key": "value",
        "list": [1, 2]
      }
      ```
    And a doc string with a media type in quotes:
      """xml
      <a>
        <b/>
      </a>
      """

  Scenario: Doc string containing delimiters
    Given text containing triple quotes:
      """
      before
      \"\"\"
      after
      """
    And text containing backticks:
      ```
      before
      \`\`\`
      after
      ```
    And triple quotes inside a backtick doc string:
      ```
      """
      not a closing delimiter
      """
      ```
    And both delimiters inside:
      """
      \"\"\"
      ```
      """
