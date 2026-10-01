package repository

import (
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"unicode"
)

type sourceValue struct {
	RawIndex int
	Text     string
}

func decodeAligned(
	metadata datasetMetadata,
	rowID int64,
	sourceRaw string,
	translationRaw string,
) ([]alignedSentence, error) {
	rawSource, err := sourceValues(sourceRaw)
	if err != nil {
		return nil, integrityError(metadata.Table, rowID, err)
	}
	source, err := normalizedSource(rawSource)
	if err != nil {
		return nil, integrityError(metadata.Table, rowID, err)
	}
	translations, err := translationValues(translationRaw)
	if err != nil {
		return nil, integrityError(metadata.Table, rowID, err)
	}
	pairs, err := alignValues(rawSource, source, translations)
	if err != nil {
		return nil, integrityError(metadata.Table, rowID, err)
	}
	return pairs, nil
}

func sourceValues(raw string) ([]any, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return nil, nil
	}
	decoder := json.NewDecoder(strings.NewReader(trimmed))
	decoder.UseNumber()
	var parsed any
	if err := decoder.Decode(&parsed); err != nil {
		if strings.HasPrefix(trimmed, "[") {
			return nil, fmt.Errorf("source array is invalid JSON")
		}
		return []any{trimmed}, nil
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return nil, fmt.Errorf("source has trailing JSON content")
	}
	if values, ok := parsed.([]any); ok {
		return values, nil
	}
	return []any{parsed}, nil
}

func normalizedSource(values []any) ([]sourceValue, error) {
	result := make([]sourceValue, 0, len(values))
	for index, value := range values {
		text := sourceText(value)
		if text != "" {
			result = append(result, sourceValue{RawIndex: index, Text: text})
		}
	}
	if len(result) == 0 {
		return nil, fmt.Errorf("source is empty")
	}
	return result, nil
}

func sourceText(value any) string {
	if value == nil {
		return ""
	}
	switch typed := value.(type) {
	case string:
		return strings.TrimSpace(typed)
	case json.Number:
		return typed.String()
	default:
		return strings.TrimSpace(fmt.Sprint(typed))
	}
}

func translationValues(raw string) ([]string, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, fmt.Errorf("translation is empty")
	}
	var parsed []any
	if err := json.Unmarshal([]byte(raw), &parsed); err != nil {
		return nil, fmt.Errorf("translation is not a JSON array")
	}
	if len(parsed) == 0 {
		return nil, fmt.Errorf("translation array is empty")
	}
	result := make([]string, len(parsed))
	for index, value := range parsed {
		text, ok := value.(string)
		if !ok {
			return nil, fmt.Errorf("translation contains a non-string value")
		}
		result[index] = strings.TrimSpace(text)
	}
	return result, nil
}

func alignValues(
	rawSource []any,
	source []sourceValue,
	translations []string,
) ([]alignedSentence, error) {
	result := make([]alignedSentence, 0, len(source))
	if len(rawSource) == len(translations) {
		for rawIndex, value := range rawSource {
			if text := sourceText(value); text != "" {
				result = appendAligned(result, rawIndex, text, translations[rawIndex])
			}
		}
	} else if len(source) == len(translations) {
		for index, value := range source {
			result = appendAligned(result, value.RawIndex, value.Text, translations[index])
		}
	} else {
		return nil, fmt.Errorf("source and translation lengths differ: %d != %d",
			len(source), len(translations))
	}
	for _, sentence := range result {
		if sentence.Translation == "" {
			return nil, fmt.Errorf("translation contains an empty aligned value")
		}
	}
	return result, nil
}

func appendAligned(
	result []alignedSentence,
	rawIndex int,
	original string,
	translation string,
) []alignedSentence {
	return append(result, alignedSentence{
		RawIndex: rawIndex, NormalizedIndex: len(result),
		Original: original, Translation: translation,
	})
}

func pairIsIndexable(original, translation string) bool {
	for _, character := range original + translation {
		if unicode.IsLetter(character) || unicode.IsNumber(character) {
			return true
		}
	}
	return false
}

func integrityError(table string, rowID int64, cause error) error {
	return fmt.Errorf("%w: %s.%d: %v", ErrDataIntegrity, table, rowID, cause)
}
