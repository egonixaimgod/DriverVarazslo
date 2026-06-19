$Session = New-Object -ComObject Microsoft.Update.Session
$Searcher = $Session.CreateUpdateSearcher()
$Searcher.ServerSelection = 3
$Searcher.ServiceID = "7971f918-a847-4430-9279-4a52d1efe18d"
$Result = $Searcher.Search("IsInstalled=0 and Type='Driver'")
Write-Output "Updates found: $($Result.Updates.Count)"
foreach ($U in $Result.Updates) {
    Write-Output " - $($U.Title)"
}
