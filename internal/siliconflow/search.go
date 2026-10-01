package siliconflow

import (
	"context"
	"math"
	"strings"
)

type embeddingRequest struct {
	Model          string   `json:"model"`
	Input          []string `json:"input"`
	EncodingFormat string   `json:"encoding_format"`
}

type embeddingResponse struct {
	Data []embeddingRow `json:"data"`
}

type embeddingRow struct {
	Index     *int       `json:"index"`
	Embedding *[]float64 `json:"embedding"`
	encoded   *string
}

type rerankRequest struct {
	Model           string   `json:"model"`
	Query           string   `json:"query"`
	Documents       []string `json:"documents"`
	TopN            int      `json:"top_n"`
	ReturnDocuments bool     `json:"return_documents"`
}

type rerankResponse struct {
	Results []rerankRow `json:"results"`
}

type rerankRow struct {
	Index *int     `json:"index"`
	Score *float64 `json:"relevance_score"`
}

type RerankResult struct {
	Index int
	Score float64
}

func (client *Client) Embed(ctx context.Context, text string) ([]float32, error) {
	vectors, err := client.EmbedMany(ctx, []string{text})
	if err != nil {
		return nil, err
	}
	return vectors[0], nil
}

func (client *Client) EmbedMany(
	ctx context.Context,
	texts []string,
) ([][]float32, error) {
	inputs, err := validateTexts("texts", texts)
	if err != nil {
		return nil, err
	}
	request := embeddingRequest{
		Model: client.config.EmbeddingModel, Input: inputs,
		EncodingFormat: client.config.EmbeddingEncoding,
	}
	var response embeddingResponse
	err = client.call(ctx, "/embeddings", request, &response,
		client.config.EmbeddingTimeout, client.embedSlots)
	if err != nil {
		return nil, err
	}
	return parseEmbeddings(response, len(inputs))
}

func (client *Client) Rerank(
	ctx context.Context,
	query string,
	documents []string,
) (RerankResult, error) {
	if strings.TrimSpace(query) == "" {
		return RerankResult{}, &ValidationError{Field: "query", Reason: "must not be empty"}
	}
	docs, err := validateTexts("documents", documents)
	if err != nil {
		return RerankResult{}, err
	}
	request := rerankRequest{
		Model: client.config.RerankModel, Query: query, Documents: docs,
		TopN: 1, ReturnDocuments: false,
	}
	var response rerankResponse
	err = client.call(ctx, "/rerank", request, &response,
		client.config.RerankTimeout, client.rerankSlots)
	if err != nil {
		return RerankResult{}, err
	}
	return parseRerank(response, len(docs))
}

func validateTexts(field string, texts []string) ([]string, error) {
	if len(texts) == 0 {
		return nil, &ValidationError{Field: field, Reason: "must not be empty"}
	}
	result := append([]string(nil), texts...)
	for _, text := range result {
		if strings.TrimSpace(text) == "" {
			return nil, &ValidationError{
				Field: field, Reason: "must contain only non-empty strings",
			}
		}
	}
	return result, nil
}

func parseEmbeddings(response embeddingResponse, expected int) ([][]float32, error) {
	if len(response.Data) != expected {
		return nil, &ResponseError{Reason: "embedding result count mismatch"}
	}
	ordered := make([][]float32, expected)
	for _, row := range response.Data {
		index, vector, err := parseEmbeddingRow(row, expected)
		if err != nil {
			return nil, err
		}
		if ordered[index] != nil {
			return nil, &ResponseError{Reason: "duplicate embedding index"}
		}
		ordered[index] = vector
	}
	for _, vector := range ordered {
		if vector == nil {
			return nil, &ResponseError{Reason: "missing embedding index"}
		}
	}
	return ordered, nil
}

func parseEmbeddingRow(row embeddingRow, expected int) (int, []float32, error) {
	if row.Index == nil {
		return 0, nil, &ResponseError{Reason: "embedding index is missing"}
	}
	if *row.Index < 0 || *row.Index >= expected {
		return 0, nil, &ResponseError{Reason: "embedding index is out of range"}
	}
	values, err := row.values()
	if err != nil {
		return 0, nil, err
	}
	if len(values) != EmbeddingDimension {
		return 0, nil, &ResponseError{Reason: "embedding must have 1024 dimensions"}
	}
	vector, err := normalize(values)
	if err != nil {
		return 0, nil, err
	}
	return *row.Index, vector, nil
}

func normalize(values []float64) ([]float32, error) {
	scale := 0.0
	for _, value := range values {
		if math.IsNaN(value) || math.IsInf(value, 0) {
			return nil, &ResponseError{Reason: "embedding values must be finite"}
		}
		scale = max(scale, math.Abs(value))
	}
	if scale == 0 {
		return nil, &ResponseError{Reason: "embedding must not be a zero vector"}
	}
	sum := 0.0
	for _, value := range values {
		scaled := value / scale
		sum += scaled * scaled
	}
	norm := scale * math.Sqrt(sum)
	if norm == 0 || math.IsNaN(norm) || math.IsInf(norm, 0) {
		return nil, &ResponseError{Reason: "embedding norm must be finite and non-zero"}
	}
	result := make([]float32, len(values))
	for index, value := range values {
		result[index] = float32(value / norm)
	}
	return result, nil
}

func parseRerank(response rerankResponse, documentCount int) (RerankResult, error) {
	if len(response.Results) != 1 {
		return RerankResult{}, &ResponseError{Reason: "rerank must contain exactly one result"}
	}
	row := response.Results[0]
	if row.Index == nil || *row.Index < 0 || *row.Index >= documentCount {
		return RerankResult{}, &ResponseError{Reason: "rerank index is missing or out of range"}
	}
	if row.Score == nil || math.IsNaN(*row.Score) || math.IsInf(*row.Score, 0) {
		return RerankResult{}, &ResponseError{Reason: "rerank score must be finite"}
	}
	return RerankResult{Index: *row.Index, Score: *row.Score}, nil
}
