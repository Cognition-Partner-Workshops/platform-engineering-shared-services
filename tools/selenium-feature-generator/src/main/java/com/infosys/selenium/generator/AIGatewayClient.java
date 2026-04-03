package com.infosys.selenium.generator;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.IOException;
import java.net.ConnectException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.channels.UnresolvedAddressException;
import java.time.Duration;

/**
 * HTTP client that communicates with the Infosys AI Gateway
 * chat/completions endpoint.
 */
public class AIGatewayClient {

    private static final String DEFAULT_ENDPOINT =
            "https://aigateway-intern.ad.infosys.com/aigateway/chat/completions";

    private final String endpoint;
    private final String apiKey;
    private final HttpClient httpClient;
    private final Gson gson;

    public AIGatewayClient(String apiKey) {
        this(apiKey, DEFAULT_ENDPOINT);
    }

    public AIGatewayClient(String apiKey, String endpoint) {
        if (apiKey == null || apiKey.isBlank()) {
            throw new IllegalArgumentException(
                    "API key must not be null or blank. "
                    + "Set the INFOSYS_CODER_API_KEY environment variable.");
        }
        this.apiKey = apiKey;
        this.endpoint = endpoint;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(30))
                .build();
        this.gson = new Gson();
    }

    /**
     * Sends a chat completion request and returns the assistant's reply.
     *
     * @param systemPrompt the system-level instruction
     * @param userPrompt   the user-level message (the user story)
     * @return the assistant's text response
     */
    public String chatCompletion(String systemPrompt, String userPrompt)
            throws IOException, InterruptedException {

        JsonObject requestBody = buildRequestBody(systemPrompt, userPrompt);

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(endpoint))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + apiKey)
                .timeout(Duration.ofSeconds(120))
                .POST(HttpRequest.BodyPublishers.ofString(requestBody.toString()))
                .build();

        HttpResponse<String> response;
        try {
            response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        } catch (ConnectException e) {
            throw new IOException(
                    "Unable to connect to AI Gateway at " + endpoint + ". "
                    + "Ensure you are on the Infosys network or VPN.", e);
        } catch (IOException e) {
            Throwable cause = e.getCause();
            if (cause instanceof UnresolvedAddressException) {
                throw new IOException(
                        "Cannot resolve AI Gateway host. "
                        + "Ensure you are on the Infosys network or VPN.", e);
            }
            throw e;
        }

        if (response.statusCode() != 200) {
            throw new IOException(
                    "AI Gateway returned HTTP " + response.statusCode()
                    + ": " + response.body());
        }

        return extractContent(response.body());
    }

    private JsonObject buildRequestBody(String systemPrompt, String userPrompt) {
        JsonObject body = new JsonObject();
        body.addProperty("model", "gpt-4");

        JsonArray messages = new JsonArray();

        JsonObject systemMsg = new JsonObject();
        systemMsg.addProperty("role", "system");
        systemMsg.addProperty("content", systemPrompt);
        messages.add(systemMsg);

        JsonObject userMsg = new JsonObject();
        userMsg.addProperty("role", "user");
        userMsg.addProperty("content", userPrompt);
        messages.add(userMsg);

        body.add("messages", messages);
        body.addProperty("temperature", 0.3);
        body.addProperty("max_tokens", 4096);

        return body;
    }

    private String extractContent(String responseJson) throws IOException {
        try {
            JsonObject root = JsonParser.parseString(responseJson).getAsJsonObject();
            JsonArray choices = root.getAsJsonArray("choices");
            if (choices == null || choices.isEmpty()) {
                throw new IOException("No choices returned from AI Gateway.");
            }
            return choices.get(0).getAsJsonObject()
                    .getAsJsonObject("message")
                    .get("content").getAsString();
        } catch (Exception e) {
            throw new IOException("Failed to parse AI Gateway response: " + e.getMessage(), e);
        }
    }
}
