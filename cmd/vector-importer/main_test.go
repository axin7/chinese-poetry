package main

import (
	"testing"

	"github.com/chinese-poetry/chinese-poetry/internal/config"
)

func TestImporterEmbeddingEncodingOptIn(t *testing.T) {
	for _, encoding := range []string{"", "float", "base64"} {
		t.Run(encoding, func(t *testing.T) {
			t.Setenv("IMPORT_EMBEDDING_ENCODING", encoding)
			settings := config.Settings{EmbeddingModel: "BAAI/bge-m3"}
			models := modelConfig(settings, "test-key")
			if models.EmbeddingEncoding != encoding || models.EmbeddingModel != settings.EmbeddingModel {
				t.Fatalf("unexpected importer encoding=%q model=%q",
					models.EmbeddingEncoding, models.EmbeddingModel)
			}
		})
	}
}
