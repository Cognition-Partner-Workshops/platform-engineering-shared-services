package com.infosys.selenium.generator;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.stream.Collectors;

/**
 * CLI entry point for the Selenium Feature-File Generator.
 *
 * <h3>Usage</h3>
 * <pre>
 *   # Read user story from a file
 *   java -jar feature-generator-1.0.0.jar --file story.txt
 *
 *   # Pass user story inline
 *   java -jar feature-generator-1.0.0.jar --story "As a user I want to ..."
 *
 *   # Read from stdin (pipe or interactive)
 *   echo "As a user ..." | java -jar feature-generator-1.0.0.jar
 *
 *   # Specify output directory (default: ./output)
 *   java -jar feature-generator-1.0.0.jar --file story.txt --output ./features
 * </pre>
 */
public class Main {

    private static final String DEFAULT_OUTPUT_DIR = "output";

    public static void main(String[] args) {
        try {
            run(args);
        } catch (Exception e) {
            System.err.println("ERROR: " + e.getMessage());
            e.printStackTrace(System.err);
            System.exit(1);
        }
    }

    private static void run(String[] args) throws Exception {
        String userStory = null;
        String outputDir = DEFAULT_OUTPUT_DIR;

        // ---- parse arguments ------------------------------------------------
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--file", "-f" -> {
                    if (++i >= args.length) throw new IllegalArgumentException("--file requires a path argument");
                    userStory = Files.readString(Path.of(args[i]));
                }
                case "--story", "-s" -> {
                    if (++i >= args.length) throw new IllegalArgumentException("--story requires a text argument");
                    userStory = args[i];
                }
                case "--output", "-o" -> {
                    if (++i >= args.length) throw new IllegalArgumentException("--output requires a path argument");
                    outputDir = args[i];
                }
                case "--help", "-h" -> {
                    printUsage();
                    return;
                }
                default -> throw new IllegalArgumentException("Unknown option: " + args[i]);
            }
        }

        // ---- read from stdin if no story was provided -----------------------
        if (userStory == null || userStory.isBlank()) {
            userStory = readStdin();
        }

        if (userStory == null || userStory.isBlank()) {
            printUsage();
            throw new IllegalArgumentException("No user story provided.");
        }

        // ---- resolve API key ------------------------------------------------
        String apiKey = System.getenv("INFOSYS_CODER_API_KEY");
        if (apiKey == null || apiKey.isBlank()) {
            throw new IllegalStateException(
                    "Environment variable INFOSYS_CODER_API_KEY is not set. "
                    + "Please export it before running the tool.");
        }

        // ---- generate -------------------------------------------------------
        System.out.println("=== Selenium Feature-File Generator ===");
        System.out.println("Reading user story (" + userStory.length() + " chars) ...");
        System.out.println("Calling Infosys AI Gateway ...");

        AIGatewayClient client = new AIGatewayClient(apiKey);
        FeatureFileGenerator generator = new FeatureFileGenerator(client);
        Path saved = generator.generateAndSave(userStory, Path.of(outputDir));

        System.out.println("Feature file generated successfully!");
        System.out.println("  -> " + saved.toAbsolutePath());
        System.out.println();
        System.out.println("--- Generated Content ---");
        System.out.println(Files.readString(saved));
    }

    private static String readStdin() throws IOException {
        if (System.in.available() > 0) {
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(System.in))) {
                return reader.lines().collect(Collectors.joining("\n"));
            }
        }
        return null;
    }

    private static void printUsage() {
        System.out.println("""
                Selenium Feature-File Generator
                ================================
                Generates Cucumber .feature files from user stories using the
                Infosys AI Gateway.

                Usage:
                  java -jar feature-generator-1.0.0.jar [OPTIONS]

                Options:
                  --file,   -f <path>   Read user story from a text file
                  --story,  -s <text>   Pass user story as an inline string
                  --output, -o <dir>    Output directory (default: ./output)
                  --help,   -h          Show this help message

                If no --file or --story is provided, the tool reads from stdin.

                Environment:
                  INFOSYS_CODER_API_KEY   Required. Your Infosys AI Gateway API key.
                """);
    }
}
