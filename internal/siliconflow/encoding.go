package siliconflow

import (
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"math"
)

func (row *embeddingRow) UnmarshalJSON(data []byte) error {
	var wire struct {
		Index     *int            `json:"index"`
		Embedding json.RawMessage `json:"embedding"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return err
	}
	*row = embeddingRow{Index: wire.Index}
	raw := bytes.TrimSpace(wire.Embedding)
	if len(raw) == 0 {
		return nil
	}
	if raw[0] == '"' {
		return json.Unmarshal(raw, &row.encoded)
	}
	return json.Unmarshal(raw, &row.Embedding)
}

func (row embeddingRow) values() ([]float64, error) {
	if row.encoded != nil {
		return decodeBase64Embedding(*row.encoded)
	}
	if row.Embedding == nil {
		return nil, nil
	}
	return *row.Embedding, nil
}

func decodeBase64Embedding(encoded string) ([]float64, error) {
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return nil, &ResponseError{Reason: "embedding is not valid base64", Err: err}
	}
	if len(data) != EmbeddingDimension*4 {
		return nil, &ResponseError{Reason: "base64 embedding must contain 1024 float32 values"}
	}
	values := make([]float64, EmbeddingDimension)
	for index := range values {
		bits := binary.LittleEndian.Uint32(data[index*4:])
		values[index] = float64(math.Float32frombits(bits))
	}
	return values, nil
}
