package main

import (
	"bufio"
	"context"
	"database/sql"
	"fmt"
	"os"
	"strings"
	"unicode/utf8"
)

const maxSQLStatementBytes = 80 * 1024

type sqlWriter struct {
	writer *bufio.Writer
	report sqlReport
}

func exportSQL(ctx context.Context, db *sql.DB, path string) (sqlReport, error) {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return sqlReport{}, err
	}
	defer file.Close()
	writer := &sqlWriter{writer: bufio.NewWriter(file)}
	for _, statement := range strings.Split(schema, ";") {
		if strings.TrimSpace(statement) != "" {
			if err := writer.statement(strings.TrimSpace(statement)); err != nil {
				return sqlReport{}, err
			}
		}
	}
	if err := dumpWorks(ctx, db, writer); err != nil {
		return sqlReport{}, err
	}
	if err := dumpMetadata(ctx, db, writer); err != nil {
		return sqlReport{}, err
	}
	if err := writer.writer.Flush(); err != nil {
		return sqlReport{}, err
	}
	if err := file.Close(); err != nil {
		return sqlReport{}, err
	}
	writer.report.fileReport, err = describeFile(path)
	return writer.report, err
}

func (writer *sqlWriter) statement(statement string) error {
	if len(statement)+2 > maxSQLStatementBytes {
		return fmt.Errorf("D1 SQL statement exceeds %d bytes", maxSQLStatementBytes)
	}
	_, err := writer.writer.WriteString(statement + ";\n")
	if err == nil {
		writer.report.Statements++
		if len(statement)+2 > writer.report.MaxStatementBytes {
			writer.report.MaxStatementBytes = len(statement) + 2
		}
	}
	return err
}

func dumpWorks(ctx context.Context, db *sql.DB, writer *sqlWriter) error {
	rows, err := db.QueryContext(ctx,
		"SELECT work_id, generation, document, locators FROM works ORDER BY work_id")
	if err != nil {
		return err
	}
	defer rows.Close()
	prefix := "INSERT INTO works (work_id,generation,document,locators) VALUES "
	statement := prefix
	for rows.Next() {
		var id, generation, document, locators string
		if err := rows.Scan(&id, &generation, &document, &locators); err != nil {
			return err
		}
		if err := writer.addWork(prefix, &statement, id, generation, document, locators); err != nil {
			return err
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	return writer.flushWorks(prefix, &statement)
}

func (writer *sqlWriter) flushWorks(prefix string, statement *string) error {
	if *statement == prefix {
		return nil
	}
	err := writer.statement(*statement)
	*statement = prefix
	return err
}

func (writer *sqlWriter) addWork(
	prefix string, statement *string, id, generation, document, locators string,
) error {
	row := "(" + sqlText(id) + "," + sqlText(generation) + "," +
		sqlText(document) + "," + sqlText(locators) + ")"
	if len(*statement)+len(row)+3 > maxSQLStatementBytes {
		if err := writer.flushWorks(prefix, statement); err != nil {
			return err
		}
	}
	if len(prefix)+len(row)+2 > maxSQLStatementBytes {
		return writer.largeWork(id, generation, document, locators)
	}
	if *statement != prefix {
		*statement += ","
	}
	*statement += row
	return nil
}

func (writer *sqlWriter) largeWork(id, generation, document, locators string) error {
	initial := "INSERT INTO works VALUES (" + sqlText(id) + "," + sqlText(generation) + ", '', '')"
	if err := writer.statement(initial); err != nil {
		return err
	}
	for _, field := range []struct{ name, value string }{
		{"document", document}, {"locators", locators},
	} {
		if err := writer.appendField(id, field.name, field.value); err != nil {
			return err
		}
	}
	return nil
}

func (writer *sqlWriter) appendField(id, column, value string) error {
	prefix := "UPDATE works SET " + column + "=" + column + " || "
	suffix := " WHERE work_id=" + sqlText(id)
	maxChunk := (maxSQLStatementBytes - len(prefix) - len(suffix) - 4) / 2
	for len(value) > 0 {
		end := min(maxChunk, len(value))
		for end < len(value) && !utf8.RuneStart(value[end]) {
			end--
		}
		if err := writer.statement(prefix + sqlText(value[:end]) + suffix); err != nil {
			return err
		}
		writer.report.ExtraWorkUpdates++
		value = value[end:]
	}
	return nil
}

func dumpMetadata(ctx context.Context, db *sql.DB, writer *sqlWriter) error {
	rows, err := db.QueryContext(ctx, "SELECT key, value FROM metadata ORDER BY key")
	if err != nil {
		return err
	}
	defer rows.Close()
	var values []string
	for rows.Next() {
		var key, value string
		if err := rows.Scan(&key, &value); err != nil {
			return err
		}
		values = append(values, "("+sqlText(key)+","+sqlText(value)+")")
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if len(values) == 0 {
		return nil
	}
	return writer.statement("INSERT INTO metadata (key,value) VALUES " + strings.Join(values, ","))
}

func sqlText(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "''") + "'"
}
