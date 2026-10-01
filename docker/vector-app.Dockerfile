FROM golang:1.25-bookworm AS build

WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY cmd ./cmd
COPY internal ./internal

RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
    -o /out/vector-api ./cmd/vector-api && \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
    -o /out/vector-importer ./cmd/vector-importer

FROM scratch

COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=build /out/vector-api /usr/local/bin/vector-api
COPY --from=build /out/vector-importer /usr/local/bin/vector-importer

CMD ["/usr/local/bin/vector-api"]
