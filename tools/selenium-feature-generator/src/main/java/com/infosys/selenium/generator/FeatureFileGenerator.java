package com.infosys.selenium.generator;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Generates Cucumber/BDD {@code .feature} files from user stories
 * by calling the Infosys AI Gateway.
 */
public class FeatureFileGenerator {

    private static final String SYSTEM_PROMPT = """
            You are an expert QA automation engineer specialising in Java Selenium
            with Cucumber BDD. Your task is to generate a complete, well-structured
            Cucumber .feature file from a given user story.

            Follow these rules strictly:
            1. Use Gherkin syntax (Feature, Background, Scenario / Scenario Outline,
               Given, When, Then, And, But, Examples).
            2. The Feature description must restate the user story in BDD "As a / I want /
               So that" format.
            3. Generate multiple Scenarios covering:
               - The happy path (primary flow)
               - Important edge cases
               - Negative / error scenarios
               - Boundary conditions where applicable
            4. Use Scenario Outline with Examples tables when data variations exist.
            5. Steps should be written from a Selenium/browser-interaction perspective
               (e.g., navigating to URLs, clicking buttons, filling forms, verifying
               page elements).
            6. Add meaningful tags (@smoke, @regression, @positive, @negative, etc.)
               to each scenario.
            7. Keep steps atomic and reusable.
            8. Output ONLY the .feature file content — no explanations, no markdown
               fences, no preamble.
            """;

    private final AIGatewayClient client;

    public FeatureFileGenerator(AIGatewayClient client) {
        this.client = client;
    }

    /**
     * Generates the Gherkin feature-file content for the supplied user story.
     *
     * @param userStory the plain-text user story
     * @return Gherkin feature-file content
     */
    public String generate(String userStory) throws IOException, InterruptedException {
        String userPrompt = "Generate a Cucumber .feature file for the following user story:\n\n"
                + userStory;
        String raw = client.chatCompletion(SYSTEM_PROMPT, userPrompt);
        return cleanResponse(raw);
    }

    /**
     * Generates the feature-file content and writes it to disk.
     *
     * @param userStory the plain-text user story
     * @param outputDir directory where the file will be written
     * @return the path of the written .feature file
     */
    public Path generateAndSave(String userStory, Path outputDir)
            throws IOException, InterruptedException {

        String content = generate(userStory);
        String fileName = deriveFileName(content) + ".feature";

        Files.createDirectories(outputDir);
        Path target = outputDir.resolve(fileName);
        Files.writeString(target, content);
        return target;
    }

    // ---- helpers --------------------------------------------------------

    /**
     * Strips markdown code fences or stray whitespace that the model
     * may include despite instructions.
     */
    private static String cleanResponse(String raw) {
        String cleaned = raw.strip();
        // Remove ```gherkin ... ``` or ``` ... ```
        if (cleaned.startsWith("```")) {
            cleaned = cleaned.replaceFirst("^```[a-zA-Z]*\\s*\n?", "");
            cleaned = cleaned.replaceFirst("\\s*```\\s*$", "");
        }
        return cleaned.strip();
    }

    /**
     * Derives a snake_case file name from the Feature title line.
     * Falls back to "generated_feature" if parsing fails.
     */
    private static String deriveFileName(String featureContent) {
        Pattern pattern = Pattern.compile("^Feature:\\s*(.+)$", Pattern.MULTILINE);
        Matcher matcher = pattern.matcher(featureContent);
        if (matcher.find()) {
            String title = matcher.group(1).trim();
            return title.toLowerCase()
                    .replaceAll("[^a-z0-9]+", "_")
                    .replaceAll("^_|_$", "");
        }
        return "generated_feature";
    }
}
