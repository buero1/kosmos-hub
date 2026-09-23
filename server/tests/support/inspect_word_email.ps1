param([Parameter(Mandatory=$true)][string]$Path)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
$document = $null
try {
    # Only the synthetic HTML supplied by the test is opened, read-only in a new instance.
    $document = $word.Documents.Open((Resolve-Path -LiteralPath $Path).Path, $false, $true, $false)
    $shapes = @(
        foreach ($shape in $document.Shapes) {
            $range = $shape.TextFrame.TextRange
            [pscustomobject]@{
                width = $shape.Width
                height = $shape.Height
                top = $shape.TextFrame.MarginTop
                bottom = $shape.TextFrame.MarginBottom
                left = $shape.TextFrame.MarginLeft
                right = $shape.TextFrame.MarginRight
                text = $range.Text.Trim()
                color = $range.Characters.Item(1).Font.Color
                font_size = $range.Characters.Item(1).Font.Size
                alignment = $range.ParagraphFormat.Alignment
                line_spacing = $range.ParagraphFormat.LineSpacing
            }
        }
    )
    ConvertTo-Json -InputObject $shapes -Compress
} finally {
    try {
        if ($null -ne $document) { $document.Close([ref]0) }
    } finally {
        $word.Quit([ref]0)
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($word)
    }
}
